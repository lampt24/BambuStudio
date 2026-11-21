#pragma once
// PrintPilotClient.hpp
// Sends POST /api/analyze, runs in Http's background thread,
// delivers results back on the wx main thread via CallAfter.
// All lifetimes are managed through shared_ptr; no raw pointers cross thread boundaries.

#include <functional>
#include <memory>
#include <string>
#include <vector>
#include <cstdint>
#include <atomic>

#include "PrintPilotConfig.hpp"

namespace Slic3r { namespace GUI { namespace AI {

// A single setting change proposed by the server.
struct SettingChange {
    std::string key;
    std::string old_value;    // stringified; informational only
    std::string new_value;    // stringified numeric from allowlist
    std::string reason;
};

// Result delivered to the main thread.
struct AnalysisResult {
    bool        success = false;
    std::string error_message;          // non-empty when !success
    std::string analysis_id;            // server-assigned; used as snapshot guard
    std::string summary;
    std::vector<std::string> warnings;
    std::vector<SettingChange> changes;
};

// Cancelled flag shared between the calling code and the Http progress callback.
struct CancelToken {
    std::atomic<bool> cancelled{false};
};

// Completion callback, always invoked on the wx main thread.
using AnalysisDoneFn = std::function<void(AnalysisResult)>;

// Kick off an async analysis request.
// project_base64 : base64-encoded 3MF (caller ensures ≤ 90 MiB on the wire).
// settings_json  : serialised DynamicPrintConfig, secrets already stripped.
// objective      : "quality"|"strength"|"speed"|"economy".
// trigger        : "manual"|"send".
// done_fn        : called on the wx thread when complete or on error/cancel.
// Returns a shared CancelToken that the caller can flip to .cancelled = true.
std::shared_ptr<CancelToken> start_analysis(
    const PrintPilotConfig&  cfg,
    const std::string&       project_base64,
    const std::string&       settings_json,
    const std::string&       objective,
    const std::string&       trigger,
    const std::string&       filename,
    AnalysisDoneFn           done_fn);

// Synchronous base64 encode of raw bytes; uses only stdlib.
std::string base64_encode(const std::uint8_t* data, std::size_t len);
std::string base64_encode_file(const std::string& path, std::size_t max_bytes, bool& truncated);

}}} // namespace Slic3r::GUI::AI
