#pragma once
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace dynabridge::pal_shell {
using id_t = std::uint64_t;
struct Event {
    id_t request = 0, session = 0, output = 0;
    std::string status, error, stdout_path, stderr_path, stdout_bytes, stderr_bytes;
    std::uint64_t stdout_total = 0, stderr_total = 0;
    int returncode = 0, signal = 0;
    bool has_returncode = false, tty = false, truncated = false, inline_output = false;
};

// Python owns this object; all operating-system/session state belongs to its reactor.
class Runtime {
public:
    explicit Runtime(unsigned completed_capacity);
    ~Runtime();
    Runtime(const Runtime&) = delete;
    Runtime& operator=(const Runtime&) = delete;
    void bind(std::function<void(const Event&)> notifier);
    void run(id_t request, std::string shell, std::string command, std::string cwd,
             bool tty, int wait_ms, int timeout_ms, int inline_limit);
    void read(id_t request, id_t session, int wait_ms);
    void write(id_t request, id_t session, std::string input);
    void resize(id_t request, id_t session, int rows, int columns);
    void terminate(id_t request, id_t session);
    void release(id_t request, id_t session);
    void release_output(id_t request, id_t output);
    void acknowledge(id_t request, id_t session, bool terminal);
    void cancel_request(id_t request, id_t target_request);
    void acquire_write(id_t request);
    void release_write(id_t request, id_t lease);
    void close();
    int callback_errors() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
