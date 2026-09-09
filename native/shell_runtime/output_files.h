#pragma once
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <fcntl.h>
#include <string>
#include <system_error>
#include <sys/stat.h>
#include <unistd.h>

namespace dynabridge::pal_shell {
// The manager owns these files until explicit result handoff/release or shutdown.
class OutputFiles {
    std::string directory_;
    void cleanup() noexcept {
        if (out >= 0) ::close(out);
        if (err >= 0) ::close(err);
        if (!stdout_path.empty()) ::unlink(stdout_path.c_str());
        if (!stderr_path.empty()) ::unlink(stderr_path.c_str());
        if (!directory_.empty()) ::rmdir(directory_.c_str());
    }
public:
    int out = -1, err = -1;
    std::string stdout_path, stderr_path;
    OutputFiles() {
        char pattern[] = "/tmp/pal-native-shell-output-XXXXXX";
        if (!mkdtemp(pattern)) throw std::system_error(errno, std::generic_category(), "output directory");
        try {
            directory_ = pattern;
            stdout_path = directory_ + "/stdout";
            stderr_path = directory_ + "/stderr";
            out = open(stdout_path.c_str(), O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
            if (out < 0) throw std::system_error(errno, std::generic_category(), "stdout file");
            err = open(stderr_path.c_str(), O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
            if (err < 0) throw std::system_error(errno, std::generic_category(), "stderr file");
        } catch (...) { cleanup(); throw; }
    }
    ~OutputFiles() { cleanup(); }
    OutputFiles(const OutputFiles&) = delete;
    OutputFiles& operator=(const OutputFiles&) = delete;
    static std::uint64_t size(int fd) {
        struct stat status{};
        if (fstat(fd, &status)) throw std::system_error(errno, std::generic_category(), "output size");
        return static_cast<std::uint64_t>(status.st_size);
    }
    static std::string read(int fd, std::size_t size) {
        std::string result(size, '\0');
        std::size_t offset = 0;
        while (offset < size) {
            auto count = pread(fd, &result[offset], size - offset, static_cast<off_t>(offset));
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) throw std::system_error(count < 0 ? errno : EIO, std::generic_category(), "output read");
            offset += static_cast<std::size_t>(count);
        }
        return result;
    }
    void append(const char* data, std::size_t size) {
        while (size) {
            const auto count = ::write(out, data, size);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) throw std::system_error(count < 0 ? errno : EIO, std::generic_category(), "PTY output file");
            data += count;
            size -= static_cast<std::size_t>(count);
        }
    }
};
}
