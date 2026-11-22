// PrintPilotClient.cpp
#include "PrintPilotClient.hpp"

#include <fstream>
#include <sstream>
#include <array>
#include <memory>
#include <stdexcept>

#include <boost/log/trivial.hpp>
#include <wx/app.h>     // wxTheApp / wxCallAfter

#include "nlohmann/json.hpp"
#include "slic3r/Utils/Http.hpp"

namespace Slic3r { namespace GUI { namespace AI {

// ---------------------------------------------------------------------------
// base64
// ---------------------------------------------------------------------------
static const char kB64Table[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string base64_encode(const std::uint8_t* data, std::size_t len)
{
    std::string out;
    out.reserve(((len + 2) / 3) * 4);
    for (std::size_t i = 0; i < len; i += 3) {
        std::uint32_t v = static_cast<std::uint32_t>(data[i]) << 16;
        if (i + 1 < len) v |= static_cast<std::uint32_t>(data[i + 1]) << 8;
        if (i + 2 < len) v |= static_cast<std::uint32_t>(data[i + 2]);
        out.push_back(kB64Table[(v >> 18) & 0x3f]);
        out.push_back(kB64Table[(v >> 12) & 0x3f]);
        out.push_back(i + 1 < len ? kB64Table[(v >>  6) & 0x3f] : '=');
        out.push_back(i + 2 < len ? kB64Table[(v >>  0) & 0x3f] : '=');
    }
    return out;
}

std::string base64_encode_file(const std::string& path,
                                std::size_t max_bytes,
                                bool& truncated)
{
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    truncated = false;
    if (!f.is_open()) return {};
    std::size_t size = static_cast<std::size_t>(f.tellg());
    if (size > max_bytes) { truncated = true; return {}; }
    f.seekg(0, std::ios::beg);
    std::vector<std::uint8_t> buf(size);
    f.read(reinterpret_cast<char*>(buf.data()), static_cast<std::streamsize>(size));
    if (static_cast<std::size_t>(f.gcount()) != size) return {};
    return base64_encode(buf.data(), static_cast<std::size_t>(f.gcount()));
}

// ---------------------------------------------------------------------------
// start_analysis
// ---------------------------------------------------------------------------
std::shared_ptr<CancelToken> start_analysis(
    const PrintPilotConfig& cfg,
    const std::string&      project_base64,
    const std::string&      settings_json,
    const std::string&      objective,
    const std::string&      trigger,
    const std::string&      filename,
    int                     plate_index,
    const std::string&      printer_serial,
    const std::string&      slice_stats_json,
    AnalysisDoneFn          done_fn)
{
    auto cancel = std::make_shared<CancelToken>();

    // Build JSON body
    nlohmann::json body;
    body["filename"]       = filename;
    body["project_base64"] = project_base64;
    body["objective"]      = objective;
    body["trigger"]        = trigger;
    body["settings"] = nlohmann::json::parse(settings_json);
    // The exported 3MF holds every plate; the server narrows geometry to this one.
    if (plate_index > 0)
        body["plate_index"] = plate_index;
    // This build can validate and apply settings_v2 changes (enums, bools, filament values).
    body["capabilities"] = nlohmann::json::array({"settings_v2"});
    if (!printer_serial.empty())
        body["printer_serial"] = printer_serial;
    if (!slice_stats_json.empty()) {
        auto stats = nlohmann::json::parse(slice_stats_json, nullptr, false);
        if (!stats.is_discarded()) body["slice_stats"] = stats;
    }
    std::string body_str = body.dump();

    // Shared state for the lambda captures; avoids dangling refs
    struct State {
        std::shared_ptr<CancelToken> cancel;
        AnalysisDoneFn               done_fn;
    };
    auto state = std::make_shared<State>(State{cancel, std::move(done_fn)});

    std::string url = cfg.server_url + "/api/analyze";
    std::string auth = "Bearer " + cfg.token;

    Http::post(url)
        .header("Authorization", auth)
        .header("Content-Type", "application/json")
        .set_post_body(body_str)
        .size_limit(1 * 1024 * 1024)   // 1 MB response limit
        .timeout_connect(10)
        .timeout_max(120)
        .on_progress([state](Http::Progress /*p*/, bool& cancel_ref) {
            if (state->cancel->cancelled.load())
                cancel_ref = true;
        })
        .on_complete([state](std::string body_resp, unsigned status) {
            AnalysisResult result;
            try {
                auto j = nlohmann::json::parse(body_resp);
                std::string srv_status = j.value("status", std::string("error"));
                if (srv_status != "complete" || status < 200 || status >= 300) {
                    result.success = false;
                    result.error_message = j.value("summary",
                        std::string("Server returned status ") + std::to_string(status));
                } else {
                    result.success      = true;
                    result.analysis_id  = j.value("id", std::string{});
                    result.summary      = j.value("summary", std::string{});
                    if (j.contains("warnings") && j["warnings"].is_array())
                        for (auto& w : j["warnings"])
                            result.warnings.push_back(w.get<std::string>());
                    if (j.contains("changes") && j["changes"].is_array()) {
                        auto text_of = [](const nlohmann::json& v) -> std::string {
                            if (v.is_number()) return v.dump();
                            if (v.is_boolean()) return v.get<bool>() ? "1" : "0";
                            if (v.is_string()) return v.get<std::string>();
                            throw std::runtime_error("Invalid setting change value");
                        };
                        for (auto& c : j["changes"]) {
                            SettingChange sc;
                            sc.key       = c.value("key",    std::string{});
                            if (!c.contains("old") || !c.contains("value"))
                                throw std::runtime_error("Invalid setting change");
                            sc.old_value = text_of(c["old"]);
                            sc.new_value = text_of(c["value"]);
                            sc.reason    = c.value("reason", std::string{});
                            sc.evidence  = c.value("evidence", std::string{});
                            sc.scope     = c.value("scope", std::string("print"));
                            sc.filament_index = c.contains("filament_index") && c["filament_index"].is_number_integer()
                                                    ? c["filament_index"].get<int>() : 0;
                            if (!sc.key.empty() && !sc.new_value.empty())
                                result.changes.push_back(std::move(sc));
                        }
                    }
                }
            } catch (const std::exception& ex) {
                result.success = false;
                result.error_message = std::string("Parse error: ") + ex.what();
            }
            // Marshal back to wx main thread
            if (state->cancel->cancelled.load()) return;
            auto fn = state->done_fn;
            auto r  = std::move(result);
            wxTheApp->CallAfter([state, fn = std::move(fn), r = std::move(r)]() mutable {
                if (state->cancel->cancelled.load()) return;
                fn(std::move(r));
            });
        })
        .on_error([state](std::string /*body*/, std::string error, unsigned status) {
            if (state->cancel->cancelled.load())
                return; // user cancelled; dialog already dismissed
            AnalysisResult result;
            result.success = false;
            result.error_message = error.empty()
                ? std::string("HTTP error ") + std::to_string(status)
                : error;
            auto fn = state->done_fn;
            auto r  = std::move(result);
            wxTheApp->CallAfter([state, fn = std::move(fn), r = std::move(r)]() mutable {
                if (state->cancel->cancelled.load()) return;
                fn(std::move(r));
            });
        })
        .perform();   // non-blocking: spins up a thread internally

    return cancel;
}

}}} // namespace Slic3r::GUI::AI
