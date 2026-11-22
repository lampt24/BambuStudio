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

// ---------------------------------------------------------------------------
// settings_v2: support / seam / infill pattern / brim type and the plate filament's
// temperatures and cooling. Mirrors services/printpilot/tuning.py SPECS.
// ---------------------------------------------------------------------------

inline bool parse_plain_number(const std::string& text, double& value)
{
    if (text.empty()) return false;
    std::istringstream stream(text);
    stream.imbue(std::locale::classic());
    stream >> std::noskipws >> value;
    return !stream.fail() && stream.peek() == std::char_traits<char>::eof() && std::isfinite(value);
}

// kind: 'b' bool (0/1), 'e' enum, 'i' integer, 'n' number. scope: 0 print preset, 1 filament preset.
struct ExtendedRule { const char* key; char kind; int scope; double low, high; const char* const* choices; };

inline const ExtendedRule* extended_rule(const std::string& key)
{
    static const char* const support_types[] = {"normal(auto)", "tree(auto)", nullptr};
    static const char* const seams[]         = {"aligned", "nearest", "back", "random", nullptr};
    static const char* const infills[]       = {"grid", "gyroid", "cubic", "crosshatch", "zig-zag", "adaptivecubic", "honeycomb", nullptr};
    static const char* const brims[]         = {"auto_brim", "outer_only", "brim_ears", "no_brim", nullptr};
    static const ExtendedRule rules[] = {
        {"enable_support", 'b', 0, 0, 1, nullptr},
        {"support_type", 'e', 0, 0, 0, support_types},
        {"support_threshold_angle", 'i', 0, 1, 90, nullptr},
        {"seam_position", 'e', 0, 0, 0, seams},
        {"sparse_infill_pattern", 'e', 0, 0, 0, infills},
        {"brim_type", 'e', 0, 0, 0, brims},
        {"outer_wall_acceleration", 'i', 0, 500, 12000, nullptr},
        {"top_surface_speed", 'n', 0, 10, 300, nullptr},
        {"initial_layer_speed", 'n', 0, 10, 100, nullptr},
        {"nozzle_temperature", 'i', 1, 170, 320, nullptr},
        {"nozzle_temperature_initial_layer", 'i', 1, 170, 320, nullptr},
        {"fan_min_speed", 'i', 1, 0, 100, nullptr},
        {"fan_max_speed", 'i', 1, 0, 100, nullptr},
        {"overhang_fan_speed", 'i', 1, 0, 100, nullptr},
        {"slow_down_layer_time", 'i', 1, 1, 60, nullptr},
        {"textured_plate_temp", 'i', 1, 0, 120, nullptr},
        {"textured_plate_temp_initial_layer", 'i', 1, 0, 120, nullptr},
        {"hot_plate_temp", 'i', 1, 0, 120, nullptr},
        {"hot_plate_temp_initial_layer", 'i', 1, 0, 120, nullptr},
        {"cool_plate_temp", 'i', 1, 0, 120, nullptr},
        {"cool_plate_temp_initial_layer", 'i', 1, 0, 120, nullptr},
        {"eng_plate_temp", 'i', 1, 0, 120, nullptr},
        {"eng_plate_temp_initial_layer", 'i', 1, 0, 120, nullptr},
    };
    for (const auto& rule : rules)
        if (key == rule.key) return &rule;
    return nullptr;
}

inline bool extended_value(const ExtendedRule& rule, const std::string& text)
{
    if (rule.kind == 'b') return text == "0" || text == "1";
    if (rule.kind == 'e') {
        for (const char* const* c = rule.choices; c && *c; ++c)
            if (text == *c) return true;
        return false;
    }
    double value;
    return parse_plain_number(text, value) && value >= rule.low && value <= rule.high &&
           (rule.kind != 'i' || std::floor(value) == value);
}

// First element of a serialized per-variant vector ("245" or "245,245"); false if they differ.
inline bool uniform_first(const std::string& serialized, std::string& first, size_t& count)
{
    count = 0;
    std::string item;
    std::istringstream stream(serialized);
    while (std::getline(stream, item, ',')) {
        if (count == 0) first = item;
        else if (item != first) return false;
        ++count;
    }
    return count > 0;
}

inline bool same_value(const ExtendedRule& rule, const std::string& a, const std::string& b)
{
    if (rule.kind == 'b' || rule.kind == 'e') return a == b;
    double x, y;
    return parse_plain_number(a, x) && parse_plain_number(b, y) && std::abs(x - y) < 0.000001;
}

// current_value: the option as serialized by the print tab, or the plate filament's single value.
inline bool valid_extended_change(const std::string& key, const std::string& old_value,
                                  const std::string& new_value, const std::string& current_value)
{
    const ExtendedRule* rule = extended_rule(key);
    return rule && extended_value(*rule, old_value) && extended_value(*rule, new_value) &&
           extended_value(*rule, current_value) && same_value(*rule, old_value, current_value);
}

}}}
