#include "process_posix.h"
#include <array>
#include <memory>
#include <cerrno>
#include <cstdlib>
#include <dirent.h>
#include <fcntl.h>
#include <signal.h>
#include <stdexcept>
#include <system_error>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>
#ifdef __APPLE__
#include <util.h>
#else
#include <pty.h>
#endif

namespace dynabridge::pal_shell {
namespace {
struct Fds {
    std::array<int, 8> values{{-1, -1, -1, -1, -1, -1, -1, -1}};
    std::size_t count = 0;
    ~Fds() { for (int fd : values) if (fd >= 0) ::close(fd); }
    void keep(int fd) { for (auto& item : values) if (item == fd) item = -1; }
    void add(int fd) {
        values.at(count++) = fd;
        if (fcntl(fd, F_SETFD, FD_CLOEXEC) < 0) throw std::system_error(errno, std::generic_category());
    }
    void pair(int first, int second) {
        values.at(count++) = first; values.at(count++) = second;
        if (fcntl(first, F_SETFD, FD_CLOEXEC) || fcntl(second, F_SETFD, FD_CLOEXEC))
            throw std::system_error(errno, std::generic_category());
    }
    void pipe(int (&pair)[2]) {
        if (::pipe(pair)) throw std::system_error(errno, std::generic_category());
        this->pair(pair[0], pair[1]);
    }
};
[[noreturn]] void fail_child(int error_fd) {
    int error = errno;
    // No Python, C++ allocation, locks, or logging in the post-fork child.
    while (::write(error_fd, &error, sizeof(error)) < 0 && errno == EINTR) {}
    _exit(127);
}
std::vector<int> inherited_fds() {
    DIR* directory = opendir("/dev/fd");
    if (!directory) throw std::system_error(errno, std::generic_category());
    std::unique_ptr<DIR, decltype(&closedir)> owned(directory, &closedir);
    std::vector<int> result;
    while (auto* entry = readdir(directory)) {
        char* end = nullptr;
        long fd = std::strtol(entry->d_name, &end, 10);
        if (end != entry->d_name && *end == '\0' && fd > 2) result.push_back(static_cast<int>(fd));
    }
    return result;
}
}

Child spawn_child(const std::string& shell, const std::string& command,
                  const std::string& cwd, bool tty, int stdout_fd, int stderr_fd) {
    Fds owned;
    int output[2] = {-1, stdout_fd}, exec_error[2];
    int input = -1;
    if (tty) {
        winsize size{}; size.ws_row = 24; size.ws_col = 80;
        if (openpty(&output[0], &output[1], nullptr, nullptr, &size))
            throw std::system_error(errno, std::generic_category());
        owned.pair(output[0], output[1]);
        input = output[1];
    } else {
        input = open("/dev/null", O_RDONLY | O_CLOEXEC);
        if (input < 0) throw std::system_error(errno, std::generic_category());
        owned.add(input);
    }
    owned.pipe(exec_error);
    const auto descriptors = inherited_fds();
    const int* close_list = descriptors.data();
    const auto close_count = descriptors.size();
    const char* shell_path = shell.c_str();
    const char* directory = cwd.c_str();
    char* const argv[] = {const_cast<char*>(shell_path), const_cast<char*>("-lc"),
                         const_cast<char*>(command.c_str()), nullptr};
    const int error_output = tty ? output[1] : stderr_fd;
    pid_t pid = fork();
    if (pid < 0) throw std::system_error(errno, std::generic_category());
    if (pid == 0) {
        sigset_t empty; sigemptyset(&empty);
        sigprocmask(SIG_SETMASK, &empty, nullptr);
        for (int signo : {SIGCHLD, SIGHUP, SIGINT, SIGQUIT, SIGTERM, SIGPIPE, SIGALRM, SIGTSTP, SIGTTIN, SIGTTOU})
            signal(signo, SIG_DFL);
        if (setsid() < 0) fail_child(exec_error[1]);
        if (dup2(input, 0) < 0 || dup2(output[1], 1) < 0 || dup2(error_output, 2) < 0)
            fail_child(exec_error[1]);
        if (tty && ioctl(0, TIOCSCTTY, 0) < 0) fail_child(exec_error[1]);
        if (*directory && chdir(directory) < 0) fail_child(exec_error[1]);
        for (std::size_t i = 0; i < close_count; ++i)
            if (close_list[i] != exec_error[1]) ::close(close_list[i]);
        execv(shell_path, argv);
        fail_child(exec_error[1]);
    }
    ::close(exec_error[1]); owned.keep(exec_error[1]);
    int error = 0;
    ssize_t received;
    do { received = ::read(exec_error[0], &error, sizeof(error)); } while (received < 0 && errno == EINTR);
    if (received != 0) {
        if (received < 0) { error = errno; kill(pid, SIGKILL); }
        while (waitpid(pid, nullptr, 0) < 0 && errno == EINTR) {}
        throw std::system_error(error ? error : EIO, std::generic_category(), "shell spawn");
    }
    if (tty) owned.keep(output[0]);
    return {pid, output[0], -1};
}

int observe_exit(pid_t pid) {
    siginfo_t info{};
    int result;
    do { result = waitid(P_PID, pid, &info, WEXITED | WNOWAIT); } while (result < 0 && errno == EINTR);
    return result == 0 ? 0 : errno;
}
int reap_child(pid_t pid) {
    int status = 0;
    pid_t result;
    do { result = waitpid(pid, &status, 0); } while (result < 0 && errno == EINTR);
    if (result < 0) throw std::system_error(errno, std::generic_category(), "reap child");
    return status;
}
void signal_child(pid_t pid, int terminal, int signo) {
    if (pid <= 0) return;
    // The leader remains waitable, reserving its PID until the manager retires it.
    if (terminal >= 0) {
        const auto foreground = tcgetpgrp(terminal);
        if (foreground > 0 && foreground != pid && foreground != getpgrp()) kill(-foreground, signo);
    }
    kill(-pid, signo);
}
}
