#pragma once
// PrintPilotConfig.hpp – load data_dir()/printpilot.json
// Owned by src/slic3r/GUI/AI/.  Read-only once initialised.

#include <string>

namespace Slic3r { namespace GUI { namespace AI {

struct PrintPilotConfig {
    std::string server_url;   // e.g. "http://100.64.0.1:8080"
    std::string token;        // Bearer token; never logged / never sent in settings payload

    bool valid() const;

    // Returns true and populates *this on success, false on missing/malformed file.
    // Silently ignores a missing config so the feature is opt-in.
    bool load();
};

}}} // namespace Slic3r::GUI::AI
