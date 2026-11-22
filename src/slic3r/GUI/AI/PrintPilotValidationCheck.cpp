#include "PrintPilotValidation.hpp"
#include <cassert>

using Slic3r::GUI::AI::valid_change;
int main()
{
    assert(valid_change("layer_height", "0.2", "0.16", "0.2", 0.4));
    assert(valid_change("sparse_infill_density", "15", "25", "15%"));
    assert(!valid_change("layer_height", "0.2", "0.4", "0.2", 0.4));
    assert(!valid_change("layer_height", "0.16", "0.2", "0.16", 0.2));
    assert(!valid_change("wall_loops", "2", "2.5", "2"));
    assert(!valid_change("layer_height", "0.2", "0.16", "0.24", 0.4));
    assert(!valid_change("gcode", "1", "2", "1"));
    assert(!valid_change("brim_width", "0", "nan", "0"));

    using Slic3r::GUI::AI::valid_extended_change;
    using Slic3r::GUI::AI::uniform_first;
    assert(valid_extended_change("enable_support", "0", "1", "0"));
    assert(!valid_extended_change("enable_support", "0", "2", "0"));
    assert(valid_extended_change("support_type", "normal(auto)", "tree(auto)", "normal(auto)"));
    assert(!valid_extended_change("support_type", "normal(auto)", "tree(manual)", "normal(auto)"));
    assert(!valid_extended_change("seam_position", "nearest", "back", "aligned"));
    assert(valid_extended_change("nozzle_temperature", "245", "250", "245"));
    assert(!valid_extended_change("nozzle_temperature", "245", "250.5", "245"));
    assert(!valid_extended_change("nozzle_temperature", "245", "400", "245"));
    assert(valid_extended_change("top_surface_speed", "50.0", "40", "50"));
    assert(!valid_extended_change("gcode", "1", "2", "1"));
    std::string first; size_t count = 0;
    assert(uniform_first("245,245", first, count) && first == "245" && count == 2);
    assert(!uniform_first("245,250", first, count));
}
