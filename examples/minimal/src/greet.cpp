#include "greet.hpp"

#include <sstream>

namespace cgw {

std::string greet(const std::string &name) {
    std::ostringstream oss;
    oss << "CGW-Hello, " << name << "!";
    return oss.str();
}

}  // namespace cgw
