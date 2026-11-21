#pragma once

#include <cmath>
#include <locale>
#include <sstream>
#include <string>

namespace Slic3r { namespace GUI { namespace AI {

inline bool numeric_setting(const std::string& key, const std::string& text,
                            double& value, bool current = false)
{
    struct Rule { const char* key; double low, high; bool integer, percent; };
    static constexpr Rule rules[] = {
        {"layer_height", 0.08, 0.28, false, false},
        {"initial_layer_print_height", 0.1, 0.3, false, false},
        {"wall_loops", 1, 10, true, false},
        {"top_shell_layers", 1, 20, true, false},
        {"bottom_shell_layers", 1, 20, true, false},
        {"sparse_infill_density", 0, 100, false, true},
        {"outer_wall_speed", 10, 200, false, false},
        {"inner_wall_speed", 10, 300, false, false},
        {"sparse_infill_speed", 10, 300, false, false},
        {"brim_width", 0, 20, false, false},
    };
    for (const auto& rule : rules) {
        if (key != rule.key) continue;
        std::string input = text;
        if (current && rule.percent && !input.empty() && input.back() == '%') input.pop_back();
        if (input.empty()) return false;
        std::istringstream stream(input);
        stream.imbue(std::locale::classic());
        stream >> std::noskipws >> value;
        return !stream.fail() && stream.peek() == std::char_traits<char>::eof() &&
               std::isfinite(value) && value >= rule.low && value <= rule.high &&
               (!rule.integer || std::floor(value) == value);
    }
    return false;
}

inline bool valid_change(const std::string& key, const std::string& old_value,
                         const std::string& new_value, const std::string& current_value,
                         double nozzle_diameter = 0)
{
    double old_number, new_number, current_number;
    return numeric_setting(key, old_value, old_number) &&
           numeric_setting(key, new_value, new_number) &&
           numeric_setting(key, current_value, current_number, true) &&
           ((key != "layer_height" && key != "initial_layer_print_height") ||
            (nozzle_diameter >= 0.1 && nozzle_diameter <= 1.2 &&
             new_number <= nozzle_diameter * 0.8 + 0.000001)) &&
           std::abs(old_number - current_number) < 0.000001;
}

}}}
