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
}
