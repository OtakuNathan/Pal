#include "runtime.h"
#include "output_files.h"
#include "process_posix.h"
#include "dynabridge/extensions/flux_foundry/python_interpreter_executor.h"
#include "dynabridge/extensions/flux_foundry/uv_executor.h"
#include <flow/flow.h>
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <deque>
#include <fcntl.h>
#include <future>
#include <map>
#include <mutex>
#include <signal.h>
#include <stdexcept>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

namespace dynabridge::pal_shell {
namespace ff = flux_foundry;
using UvExecutor = dynabridge::flux_foundry_extensions::uv_executor;
using GilExecutor = dynabridge::flux_foundry_extensions::python_interpreter_executor;
static std::atomic<id_t> next_id{1};

struct Runtime::Impl {
    static thread_local Impl* delivering;
    struct Session;
    struct ProcessAwaitable;
    struct Poll { uv_poll_t handle{}; std::shared_ptr<Session> session; int fd; bool error; };
    enum class TimerKind { wait, deadline, kill };
    struct Timer {
        uv_timer_t handle{};
        std::shared_ptr<Session> session;
        TimerKind kind;
        id_t request;
        bool initial;
    };
    struct Session {
        Impl* owner;
        id_t id = next_id++;
        std::string shell, command, cwd, reason, error;
        bool tty = false, exposed = false, delivered = false, exit_seen = false, settled = false;
        bool flow_done = false, finished = false;
        bool result_delivered = false;
        int wait_ms = 0, timeout_ms = 0, returncode = 0, signal = 0, inline_limit = 0;
        pid_t pid = -1;
        id_t initial_request;
        std::unique_ptr<OutputFiles> files;
        std::string input;
        Poll* output = nullptr;
        Poll* errors = nullptr;
        std::vector<Timer*> timers;
        std::thread waiter;
        ff::lite_ptr<ff::flow_controller> controller = ff::make_lite_ptr<ff::flow_controller>();
        ProcessAwaitable* awaitable = nullptr;
        explicit Session(Impl* owner_, id_t request) : owner(owner_), initial_request(request) {}
    };
    using Result = ff::result_t<int, std::exception_ptr>;
    struct ProcessAwaitable final : ff::awaitable_base<ProcessAwaitable, int, std::exception_ptr> {
        using async_result_type = Result;
        std::shared_ptr<Session> session;
        explicit ProcessAwaitable(ff::result_t<std::shared_ptr<Session>, std::exception_ptr>&& value)
            : session(std::move(value).value()) {}
        int submit() noexcept {
            retain();
            session->awaitable = this;
            try { session->owner->spawn(session); }
            catch (const std::exception& e) {
                session->error = e.what();
                session->awaitable = nullptr;
                session->settled = true;
                release();
                return -1;
            }
            return 0;
        }
        void cancel() noexcept { session->owner->request_stop(session); }
    };
    struct Receiver {
        using value_type = Result;
        std::shared_ptr<Session> session;
        void emplace(Result&&) noexcept {
            session->flow_done = true;
            session->owner->complete(session);
        }
    };

    using Blueprint = decltype(ff::make_blueprint<std::shared_ptr<Session>>()
        | ff::via(static_cast<UvExecutor*>(nullptr))
        | ff::await<ProcessAwaitable>(static_cast<UvExecutor*>(nullptr)) | ff::end());

    uv_loop_t loop{};
    UvExecutor* executor = nullptr; // Constructed and closed on its owning loop thread.
    ff::lite_ptr<Blueprint> blueprint; // Immutable graph; executor belongs to this runtime.
    GilExecutor gil;
    std::thread reactor;
    std::mutex ingress;
    bool closed = false, stopping = false;
    std::atomic<int> notification_errors{0};
    std::function<void(const Event&)> notifier;
    std::map<id_t, std::shared_ptr<Session>> sessions;
    std::deque<id_t> completed;
    id_t writer = 0;
    unsigned completed_capacity;

    explicit Impl(unsigned capacity) : completed_capacity(capacity) {
        if (capacity < 1 || capacity > 32) throw std::invalid_argument("completed capacity must be 1..32");
        std::promise<void> ready;
        auto started = ready.get_future();
        reactor = std::thread([this, &ready] {
            bool announced = false;
            try {
                if (uv_loop_init(&loop)) throw std::runtime_error("uv_loop_init failed");
                UvExecutor local(&loop);
                executor = &local;
                auto bp = ff::make_blueprint<std::shared_ptr<Session>>()
                    | ff::via(executor) | ff::await<ProcessAwaitable>(executor) | ff::end();
                blueprint = ff::make_lite_ptr<Blueprint>(std::move(bp));
                announced = true;
                ready.set_value();
                uv_run(&loop, UV_RUN_DEFAULT);
                executor = nullptr;
                if (uv_loop_close(&loop)) std::terminate();
            } catch (...) {
                if (announced) std::terminate(); // Constructor no longer owns the promise.
                ready.set_exception(std::current_exception());
            }
        });
        try { started.get(); } catch (...) { reactor.join(); throw; }
    }
    ~Impl() { shutdown(); }

    void emit(Event event) {
        gil.dispatch(ff::task_wrapper_sbo([this, event = std::move(event)]() noexcept {
            delivering = this;
            try { if (notifier) notifier(event); }
            catch (...) { ++notification_errors; PyErr_Clear(); }
            delivering = nullptr;
        }));
    }
    void fail(id_t request, const std::string& error) {
        Event event; event.request = request; event.status = "rejected"; event.error = error;
        emit(std::move(event));
    }
    template<class F> void post(id_t request, F work) {
        std::lock_guard<std::mutex> lock(ingress);
        if (closed) throw std::runtime_error("shell runtime is closed");
        if (!notifier) throw std::runtime_error("bind a callback before submitting work");
        executor->dispatch(ff::task_wrapper_sbo([this, request, work = std::move(work)]() noexcept {
            try { work(); } catch (const std::exception& e) { fail(request, e.what()); }
        }));
    }
    std::shared_ptr<Session> lookup(id_t id) {
        const auto found = sessions.find(id);
        if (found == sessions.end() || !found->second->exposed)
            throw std::runtime_error("invalid_session: unknown or retired runtime/session");
        return found->second;
    }
    Event snapshot(const std::shared_ptr<Session>& s, id_t request, bool initial = false) {
        Event event;
        event.request = request;
        event.session = initial && s->finished ? 0 : s->id;
        event.tty = s->tty;
        event.status = s->finished ? (s->error.empty() ? (s->reason.empty() ? "exited" : s->reason) : "failed")
                                   : (s->reason.empty() ? "running" : "terminating");
        event.error = s->error;
        event.output = s->id;
        if (s->files) {
            event.stdout_path = s->files->stdout_path; event.stderr_path = s->files->stderr_path;
            event.stdout_total = OutputFiles::size(s->files->out);
            event.stderr_total = OutputFiles::size(s->files->err);
            if (s->inline_limit < 0 || event.stdout_total + event.stderr_total <= static_cast<unsigned>(s->inline_limit)) {
                event.stdout_bytes = OutputFiles::read(s->files->out, event.stdout_total);
                event.stderr_bytes = OutputFiles::read(s->files->err, event.stderr_total);
                event.inline_output = true;
            }
        }
        event.has_returncode = s->finished && s->pid > 0;
        event.returncode = s->returncode; event.signal = s->signal;
        return event;
    }
    static void close_poll(Poll*& poll) {
        if (!poll) return;
        uv_poll_stop(&poll->handle);
        uv_close(reinterpret_cast<uv_handle_t*>(&poll->handle), [](uv_handle_t* handle) {
            auto* p = static_cast<Poll*>(handle->data); ::close(p->fd); delete p;
        });
        poll = nullptr;
    }
    static void close_timer(Timer* timer) {
        auto& timers = timer->session->timers;
        timers.erase(std::remove(timers.begin(), timers.end(), timer), timers.end());
        uv_timer_stop(&timer->handle);
        uv_close(reinterpret_cast<uv_handle_t*>(&timer->handle), [](uv_handle_t* handle) {
            delete static_cast<Timer*>(handle->data);
        });
    }
    void timer(const std::shared_ptr<Session>& s, TimerKind kind, int ms,
               id_t request = 0, bool initial = false) {
        auto* value = new Timer{{}, s, kind, request, initial};
        int code = uv_timer_init(&loop, &value->handle);
        if (code) { delete value; throw std::runtime_error(uv_strerror(code)); }
        value->handle.data = value;
        s->timers.push_back(value);
        uv_update_time(&loop);
        code = uv_timer_start(&value->handle, [](uv_timer_t* handle) {
            auto* t = static_cast<Timer*>(handle->data);
            auto session = t->session;
            auto* owner = session->owner;
            const auto kind = t->kind;
            if (kind == TimerKind::wait) {
                if (t->initial) session->exposed = true;
                owner->emit(owner->snapshot(session, t->request, t->initial));
            }
            close_timer(t);
            if (kind == TimerKind::deadline && !session->settled) {
                if (session->reason.empty()) session->reason = "timed_out";
                session->controller->cancel(true);
            } else if (kind == TimerKind::kill && !session->settled) {
                signal_child(session->pid, session->tty && session->output ? session->output->fd : -1, SIGKILL);
            }
        }, ms, 0);
        if (code) { close_timer(value); throw std::runtime_error(uv_strerror(code)); }
    }
    void arm(Poll* p) {
        int events = UV_READABLE;
        if (!p->error && p->session->tty && !p->session->input.empty()) events |= UV_WRITABLE;
        int code = uv_poll_start(&p->handle, events, [](uv_poll_t* handle, int status, int events) {
            auto* poll = static_cast<Poll*>(handle->data);
            auto session = poll->session;
            auto* owner = session->owner;
            if ((events & UV_WRITABLE) && !session->input.empty()) {
                ssize_t count = ::write(poll->fd, session->input.data(), session->input.size());
                if (count > 0) session->input.erase(0, static_cast<std::size_t>(count));
                else if (count < 0 && errno != EINTR && errno != EAGAIN) {
                    session->error = "PTY input failed"; session->input.clear();
                    session->controller->cancel(true);
                }
            }
            bool eof = status < 0;
            if ((events & UV_READABLE) || status < 0) {
                char bytes[8192];
                for (int round = 0; round < 32; ++round) {
                    const auto count = ::read(poll->fd, bytes, sizeof(bytes));
                    if (count > 0) {
                        try { if (session->error.empty()) session->files->append(bytes, count); }
                        catch (const std::exception& e) {
                            session->error = e.what();
                            session->controller->cancel(true);
                        }
                    }
                    else if (count < 0 && errno == EINTR) continue;
                    else { eof = count == 0 || (errno != EAGAIN && errno != EWOULDBLOCK); break; }
                }
            }
            if (eof) {
                if (poll->error) close_poll(session->errors); else close_poll(session->output);
                owner->settle(session);
            } else owner->arm(poll);
        });
        if (code) throw std::runtime_error(uv_strerror(code));
    }
    Poll* poll(const std::shared_ptr<Session>& s, int fd, bool error) {
        auto* value = new Poll{{}, s, fd, error};
        if (fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK) < 0) {
            delete value; ::close(fd); throw std::runtime_error("nonblocking setup failed");
        }
        int code = uv_poll_init(&loop, &value->handle, fd);
        if (code) { delete value; ::close(fd); throw std::runtime_error(uv_strerror(code)); }
        value->handle.data = value;
        try { arm(value); }
        catch (...) { close_poll(value); throw; }
        return value;
    }
    void spawn(const std::shared_ptr<Session>& s) {
        s->files = std::make_unique<OutputFiles>();
        Child child = spawn_child(s->shell, s->command, s->cwd, s->tty, s->files->out, s->files->err);
        s->pid = child.pid;
        try {
            if (child.output >= 0) s->output = poll(s, std::exchange(child.output, -1), false);
            if (child.error >= 0) s->errors = poll(s, std::exchange(child.error, -1), true);
            timer(s, TimerKind::wait, s->wait_ms, s->initial_request, true);
            if (s->timeout_ms) timer(s, TimerKind::deadline, s->timeout_ms);
            s->waiter = std::thread([this, s] {
                const int error = observe_exit(s->pid);
                executor->dispatch(ff::task_wrapper_sbo([this, s, error]() noexcept {
                    s->waiter.join();
                    if (error) s->error = "waitid failed: " + std::to_string(error);
                    s->exit_seen = true;
                    // Retain the unreaped leader while retiring any surviving group members.
                    signal_child(s->pid, s->tty && s->output ? s->output->fd : -1, SIGKILL);
                    settle(s);
                }));
            });
        } catch (...) {
            signal_child(s->pid, s->tty && s->output ? s->output->fd : -1, SIGKILL);
            reap_child(s->pid);
            if (child.output >= 0) ::close(child.output);
            if (child.error >= 0) ::close(child.error);
            close_poll(s->output); close_poll(s->errors);
            for (auto* t : std::vector<Timer*>(s->timers)) close_timer(t);
            throw;
        }
    }
    void request_stop(const std::shared_ptr<Session>& s) noexcept {
        if (s->settled) return;
        if (s->reason.empty()) s->reason = "cancelled";
        signal_child(s->pid, s->tty && s->output ? s->output->fd : -1, SIGTERM);
        try { timer(s, TimerKind::kill, 1000); }
        catch (...) { signal_child(s->pid, s->tty && s->output ? s->output->fd : -1, SIGKILL); }
    }
    void settle(const std::shared_ptr<Session>& s) {
        if (s->settled || !s->exit_seen || s->output || s->errors) return;
        try {
            const auto status = reap_child(s->pid);
            s->signal = WIFSIGNALED(status) ? WTERMSIG(status) : 0;
            s->returncode = s->signal ? -s->signal : WEXITSTATUS(status);
        } catch (const std::exception& e) { s->error = e.what(); }
        s->settled = true;
        auto* awaitable = std::exchange(s->awaitable, nullptr);
        if (awaitable) {
            awaitable->resume(Result(ff::value_tag, s->returncode));
            awaitable->release();
        }
        complete(s);
    }
    void complete(const std::shared_ptr<Session>& s) noexcept {
        if (!s->settled || !s->flow_done || s->finished) return;
        s->finished = true;
        if (writer == s->id) writer = 0;
        bool initial_delivered = s->exposed;
        for (auto* t : std::vector<Timer*>(s->timers)) {
            if (t->kind == TimerKind::wait) {
                emit(snapshot(s, t->request, t->initial));
                if (t->initial) initial_delivered = true;
            }
            close_timer(t);
        }
        if (!initial_delivered) emit(snapshot(s, s->initial_request, true));
        if (s->exposed) {
            emit(snapshot(s, 0));
        }
        completed.push_back(s->id);
        maybe_shutdown();
    }
    void begin(const std::shared_ptr<Session>& s) {
        if (writer) throw std::runtime_error("write_busy: a shell session or write operation owns this context");
        while (completed.size() >= completed_capacity) {
            auto item = std::find_if(completed.begin(), completed.end(), [this](id_t id) {
                return sessions.at(id)->result_delivered;
            });
            if (item == completed.end()) throw std::runtime_error("result_capacity: acknowledge or release completed results");
            sessions.erase(*item); completed.erase(item);
        }
        ff::flow_runner<Blueprint, Receiver> runner(blueprint, s->controller, Receiver{s});
        sessions.emplace(s->id, s);
        writer = s->id;
        runner(s);
    }
    void maybe_shutdown() {
        if (!stopping) return;
        for (const auto& item : sessions) if (!item.second->finished) return;
        sessions.clear(); completed.clear();
        executor->close();
    }
    void shutdown() {
        {
            std::lock_guard<std::mutex> lock(ingress);
            if (closed) return;
            closed = true;
            executor->dispatch(ff::task_wrapper_sbo([this]() noexcept {
                stopping = true;
                auto current = sessions;
                for (const auto& item : current) if (!item.second->finished) item.second->controller->cancel(true);
                maybe_shutdown();
            }));
        }
        // Neither native cleanup nor the GIL worker waits for asyncio delivery.
        PyThreadState* saved = PyGILState_Check() ? PyEval_SaveThread() : nullptr;
        reactor.join();
        if (saved) PyEval_RestoreThread(saved);
        gil.dispatch(ff::task_wrapper_sbo([this]() noexcept { notifier = {}; }));
        gil.shutdown();
    }
};

thread_local Runtime::Impl* Runtime::Impl::delivering = nullptr;

Runtime::Runtime(unsigned capacity) : impl_(std::make_unique<Impl>(capacity)) {}
Runtime::~Runtime() = default;
void Runtime::bind(std::function<void(const Event&)> notifier) {
    std::lock_guard<std::mutex> lock(impl_->ingress);
    if (impl_->closed || impl_->notifier) throw std::runtime_error("callback already bound or runtime closed");
    impl_->notifier = std::move(notifier);
}
void Runtime::run(id_t request, std::string shell, std::string command, std::string cwd,
                  bool tty, int wait_ms, int timeout_ms, int inline_limit) {
    if (inline_limit < -1) throw std::invalid_argument("invalid output budget");
    if (wait_ms < 0 || timeout_ms < 0) throw std::invalid_argument("negative timeout");
    for (const auto* value : {&shell, &command, &cwd})
        if (value->find('\0') != std::string::npos) throw std::invalid_argument("NUL in command/path");
    if (shell.empty() || shell.front() != '/' || command.empty()) throw std::invalid_argument("absolute shell path and command required");
    auto session = std::make_shared<Impl::Session>(impl_.get(), request);
    session->shell = std::move(shell); session->command = std::move(command); session->cwd = std::move(cwd);
    session->inline_limit = inline_limit;
    session->tty = tty; session->wait_ms = wait_ms; session->timeout_ms = timeout_ms;
    impl_->post(request, [owner = impl_.get(), session] { owner->begin(session); });
}
void Runtime::read(id_t request, id_t id, int wait_ms) {
    if (wait_ms < 0) throw std::invalid_argument("negative wait budget");
    impl_->post(request, [=] {
        auto session = impl_->lookup(id);
        if (session->finished) impl_->emit(impl_->snapshot(session, request));
        else impl_->timer(session, Impl::TimerKind::wait, wait_ms, request);
    });
}
void Runtime::write(id_t request, id_t id, std::string input) {
    impl_->post(request, [this, request, id, input = std::move(input)] {
        auto session = impl_->lookup(id);
        if (!session->tty || !session->output || session->exit_seen || !session->reason.empty())
            throw std::runtime_error("stdin_closed: no writable PTY");
        if (session->input.size() + input.size() > 65536) throw std::runtime_error("input buffer full");
        session->input += input;
        impl_->arm(session->output);
        auto event = impl_->snapshot(session, request); event.status = "input_accepted";
        impl_->emit(std::move(event));
    });
}
void Runtime::resize(id_t request, id_t id, int rows, int columns) {
    if (rows < 1 || columns < 1 || rows > 65535 || columns > 65535) throw std::invalid_argument("invalid terminal size");
    impl_->post(request, [=] {
        auto session = impl_->lookup(id);
        if (!session->tty || !session->output || session->exit_seen) throw std::runtime_error("no live PTY");
        winsize size{}; size.ws_row = rows; size.ws_col = columns;
        if (ioctl(session->output->fd, TIOCSWINSZ, &size)) throw std::runtime_error("resize failed");
        impl_->emit(impl_->snapshot(session, request));
    });
}
void Runtime::terminate(id_t request, id_t id) {
    impl_->post(request, [=] {
        auto session = impl_->lookup(id);
        if (!session->finished) session->controller->cancel(true);
        impl_->emit(impl_->snapshot(session, request));
    });
}
void Runtime::release(id_t request, id_t id) {
    impl_->post(request, [=] {
        auto session = impl_->lookup(id);
        if (!session->finished) throw std::runtime_error("session still running; terminate first");
        impl_->sessions.erase(id);
        auto& done = impl_->completed;
        done.erase(std::remove(done.begin(), done.end(), id), done.end());
        Event event; event.request = request; event.session = id; event.status = "released"; impl_->emit(std::move(event));
    });
}
void Runtime::release_output(id_t request, id_t id) {
    impl_->post(request, [=] {
        auto found = impl_->sessions.find(id);
        // Handoff acknowledgement can be retried after the first reply is lost.
        if (found != impl_->sessions.end()) {
            if (!found->second->finished) throw std::runtime_error("output is still being written");
            impl_->sessions.erase(found);
        }
        auto& done = impl_->completed;
        done.erase(std::remove(done.begin(), done.end(), id), done.end());
        Event event; event.request = request; event.status = "output_released";
        impl_->emit(std::move(event));
    });
}
void Runtime::acknowledge(id_t request, id_t id, bool terminal) {
    impl_->post(request, [=] {
        auto session = impl_->lookup(id);
        session->delivered = true;
        if (terminal && session->finished) session->result_delivered = true;
        Event event; event.request = request; event.session = id; event.status = "acknowledged";
        impl_->emit(std::move(event));
    });
}
void Runtime::cancel_request(id_t request, id_t target) {
    impl_->post(request, [=] {
        auto current = impl_->sessions;
        for (const auto& item : current) {
            auto session = item.second;
            if (session->initial_request == target && !session->delivered && !session->finished)
                session->controller->cancel(true);
            for (auto* timer : std::vector<Impl::Timer*>(session->timers)) {
                if (timer->kind == Impl::TimerKind::wait && timer->request == target && !timer->initial) {
                    auto event = impl_->snapshot(session, target); event.status = "wait_cancelled";
                    impl_->emit(std::move(event)); Impl::close_timer(timer);
                }
            }
        }
        Event event; event.request = request; event.status = "cancel_requested";
        impl_->emit(std::move(event));
    });
}
void Runtime::acquire_write(id_t request) {
    impl_->post(request, [=] {
        if (impl_->writer) throw std::runtime_error("write_busy: shell session or write operation active");
        impl_->writer = next_id++;
        Event event; event.request = request; event.session = impl_->writer; event.status = "write_acquired";
        impl_->emit(std::move(event));
    });
}
void Runtime::release_write(id_t request, id_t lease) {
    impl_->post(request, [=] {
        if (impl_->writer != lease || impl_->sessions.count(lease)) throw std::runtime_error("invalid write lease");
        impl_->writer = 0;
        Event event; event.request = request; event.status = "write_released"; impl_->emit(std::move(event));
    });
}
void Runtime::close() {
    if (Impl::delivering == impl_.get())
        throw std::runtime_error("close must be scheduled on the host thread, outside the native callback");
    impl_->shutdown();
}
int Runtime::callback_errors() const { return impl_->notification_errors.load(); }
}
