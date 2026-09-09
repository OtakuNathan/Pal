#pragma once
#include <string>
#include <sys/types.h>

namespace dynabridge::pal_shell {
struct Child {
    pid_t pid = -1;
    int output = -1, error = -1;
};
// The caller keeps the leader unreaped until all signalling is finished.
Child spawn_child(const std::string& shell, const std::string& command,
                  const std::string& cwd, bool tty, int stdout_fd, int stderr_fd);
int observe_exit(pid_t pid); // waitid(WNOWAIT), no global SIGCHLD handler.
int reap_child(pid_t pid);
void signal_child(pid_t pid, int terminal, int signal);
}
