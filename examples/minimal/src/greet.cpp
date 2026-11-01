#include "greet.hpp"

#include <sstream>

namespace ccu {

std::string greet(const std::string &name) {
    std::ostringstream oss;
    oss << "CCU-Hello, " << name << "!";
    return oss.str();
}

}  // namespace ccu
