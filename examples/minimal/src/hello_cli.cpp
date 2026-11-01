#include <iostream>
#include <string>

#include "greet.hpp"

int main(int argc, char *argv[]) {
    std::string name = (argc > 1) ? argv[1] : "CCU";
    std::cout << ccu::greet(name) << std::endl;
    return 0;
}
