// PrintPilotDialog.cpp
#include "PrintPilotDialog.hpp"
#include "PrintPilotConfig.hpp"
#include "PrintPilotClient.hpp"
#include "PrintPilotValidation.hpp"

#include <algorithm>
#include <cmath>
#include <set>
#include <stdexcept>

#include <boost/filesystem/path.hpp>
#include <boost/filesystem/operations.hpp>
#include <boost/log/trivial.hpp>

#include <wx/button.h>
#include <wx/gauge.h>
#include <wx/sizer.h>
#include <wx/stattext.h>
#include <wx/scrolwin.h>
#include <wx/checkbox.h>
#include <wx/timer.h>
#include <wx/event.h>
#include <wx/msgdlg.h>
#include <wx/settings.h>
#include <wx/font.h>
#include <wx/panel.h>
#include <wx/timer.h>
#include <wx/display.h>

#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/Plater.hpp"
#include "slic3r/GUI/MainFrame.hpp"
#include "slic3r/GUI/Tab.hpp"
#include "slic3r/GUI/PartPlate.hpp"
#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "libslic3r/ExtrusionEntity.hpp"
#include "slic3r/GUI/DeviceManager.hpp"
#include "slic3r/GUI/DeviceCore/DevManager.h"
#include "libslic3r/Preset.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "nlohmann/json.hpp"

namespace Slic3r { namespace GUI { namespace AI {

namespace {
// Size a dialog to its content, in DPI-independent units, but never taller than
// 85% (or wider than 90%) of the screen it opens on; the content area scrolls.
void fit_to_screen(wxDialog* dlg, wxWindow* parent, int min_w_dip, int min_h_dip)
{
    dlg->Layout();
    const wxSize best = dlg->GetBestSize();
    const int display = wxDisplay::GetFromWindow(parent ? parent : dlg);
    const wxRect area = wxDisplay(display == wxNOT_FOUND ? 0u : unsigned(display)).GetClientArea();
    const int max_w = area.GetWidth() * 9 / 10, max_h = area.GetHeight() * 85 / 100;
    const wxSize min_size(std::min(dlg->FromDIP(min_w_dip), max_w), std::min(dlg->FromDIP(min_h_dip), max_h));
    dlg->SetMinSize(min_size);
    dlg->SetSize(wxSize(std::clamp(best.GetWidth(), min_size.GetWidth(), max_w),
                        std::clamp(best.GetHeight(), min_size.GetHeight(), max_h)));
    dlg->Layout();
    dlg->CentreOnParent(wxBOTH);
}
} // namespace

// ===========================================================================
// PrintPilotProgressDialog
// ===========================================================================
PrintPilotProgressDialog::PrintPilotProgressDialog(wxWindow* parent,
                                                   const std::string& trigger)
    : wxDialog(parent, wxID_ANY, _("PrintPilot AI Analysis"),
               wxDefaultPosition, wxDefaultSize,
               wxDEFAULT_DIALOG_STYLE & ~wxCLOSE_BOX)
    , m_pulse_timer(this)
{
    auto* top = new wxBoxSizer(wxVERTICAL);

    wxString label = trigger == "send"
        ? _("Analysing settings before send…")
        : _("Analysing settings…");
    auto* lbl = new wxStaticText(this, wxID_ANY, label);
    lbl->SetFont(lbl->GetFont().MakeBold());
    top->Add(lbl, 0, wxALL | wxEXPAND, 12);

    m_gauge = new wxGauge(this, wxID_ANY, 100,
                          wxDefaultPosition, wxSize(-1, FromDIP(8)), wxGA_HORIZONTAL);
    top->Add(m_gauge, 0, wxLEFT | wxRIGHT | wxBOTTOM | wxEXPAND, 12);

        auto* note = new wxStaticText(this, wxID_ANY,
        _("You can cancel while analysis runs."));
    note->SetForegroundColour(wxSystemSettings::GetColour(wxSYS_COLOUR_GRAYTEXT));
    top->Add(note, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);

    auto* cancel_btn = new wxButton(this, wxID_CANCEL, _("Cancel"));
    top->Add(cancel_btn, 0, wxALIGN_CENTER | wxBOTTOM, 12);

    SetSizer(top);
    fit_to_screen(this, parent, 400, 160);

    cancel_btn->Bind(wxEVT_BUTTON, &PrintPilotProgressDialog::on_cancel_btn, this);
    Bind(wxEVT_TIMER, &PrintPilotProgressDialog::on_timer, this);
    m_pulse_timer.Start(80);
}

void PrintPilotProgressDialog::on_cancel_btn(wxCommandEvent&)
{
    m_user_cancelled = true;
    m_pulse_timer.Stop();
    EndModal(wxID_CANCEL);
}

void PrintPilotProgressDialog::on_timer(wxTimerEvent&)
{
    if (m_finished) return;
    m_gauge->Pulse();
}

void PrintPilotProgressDialog::finish()
{
    m_finished = true;
    m_pulse_timer.Stop();
    m_gauge->SetValue(100);
    EndModal(wxID_OK);
}

void PrintPilotProgressDialog::cancel_external()
{
    m_finished = true;
    m_pulse_timer.Stop();
    EndModal(wxID_CANCEL);
}

// ===========================================================================
// PrintPilotResultDialog
// ===========================================================================
PrintPilotResultDialog::PrintPilotResultDialog(wxWindow* parent,
                                               const AnalysisResult& result,
                                               const std::vector<SettingChange>& allowed,
                                               const std::vector<SettingChange>& manual)
    : wxDialog(parent, wxID_ANY, _("PrintPilot: AI Suggestions"),
               wxDefaultPosition, wxDefaultSize,
               wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER)
{
    auto* top = new wxBoxSizer(wxVERTICAL);
    const int wrap = FromDIP(560);

    // Summary, warnings and changes share one scrolled area so the buttons below
    // stay on screen however long the analysis is.
    auto* scroll = new wxScrolledWindow(this, wxID_ANY, wxDefaultPosition, wxDefaultSize, wxVSCROLL);
    scroll->SetScrollRate(0, FromDIP(10));
    auto* sv = new wxBoxSizer(wxVERTICAL);

    if (!result.summary.empty()) {
        auto* lbl = new wxStaticText(scroll, wxID_ANY, wxString::FromUTF8(result.summary));
        lbl->Wrap(wrap);
        sv->Add(lbl, 0, wxALL | wxEXPAND, FromDIP(10));
    }

    for (auto& w : result.warnings) {
        auto* wlbl = new wxStaticText(scroll, wxID_ANY,
            wxString::FromUTF8("\xE2\x9A\xA0 ") + wxString::FromUTF8(w));
        wlbl->SetForegroundColour(wxColour(200, 120, 0));
        wlbl->Wrap(wrap);
        sv->Add(wlbl, 0, wxLEFT | wxRIGHT | wxBOTTOM | wxEXPAND, FromDIP(8));
    }

    if (allowed.empty() && manual.empty()) {
        auto* none = new wxStaticText(scroll, wxID_ANY,
            _("No applicable settings changes were proposed."));
        none->SetForegroundColour(wxSystemSettings::GetColour(wxSYS_COLOUR_GRAYTEXT));
        sv->Add(none, 0, wxALL, FromDIP(10));
    } else {
        auto* hdr = new wxStaticText(scroll, wxID_ANY,
            _("Proposed changes (allowlisted settings only):"));
        hdr->SetFont(hdr->GetFont().MakeBold());
        sv->Add(hdr, 0, wxLEFT | wxRIGHT | wxTOP, FromDIP(10));

        auto add_rows = [&](const std::vector<SettingChange>& rows, bool by_hand) {
            for (auto& ch : rows) {
                wxString row = wxString::Format(
                    "%s%s:  %s  %s  %s%s",
                    wxString::FromUTF8(ch.key),
                    ch.scope == "filament" ? wxString::Format(" (filament %d)", ch.filament_index) : wxString(),
                    wxString::FromUTF8(ch.old_value),
                    wxString::FromUTF8("\xE2\x86\x92"),
                    wxString::FromUTF8(ch.new_value),
                    by_hand ? wxString("   ") + _("(set by hand)") : wxString());
                auto* row_lbl = new wxStaticText(scroll, wxID_ANY, row);
                row_lbl->Wrap(wrap);
                sv->Add(row_lbl, 0, wxLEFT | wxRIGHT | wxTOP, FromDIP(10));

                for (const std::string* text : {&ch.reason, &ch.evidence}) {
                    if (text->empty()) continue;
                    auto* lbl = new wxStaticText(scroll, wxID_ANY,
                        wxString::FromUTF8(text == &ch.evidence ? "\xE2\x93\x98 " : "") + wxString::FromUTF8(*text));
                    lbl->SetForegroundColour(wxSystemSettings::GetColour(wxSYS_COLOUR_GRAYTEXT));
                    lbl->Wrap(wrap - FromDIP(20));
                    sv->Add(lbl, 0, wxLEFT | wxRIGHT, FromDIP(28));
                }
            }
        };
        add_rows(allowed, false);
        add_rows(manual, true);
    }
    sv->AddSpacer(FromDIP(10));
    scroll->SetSizer(sv);
    scroll->FitInside();
    // Ask for the full content height so the dialog opens big enough; fit_to_screen caps it.
    scroll->SetMinSize(sv->GetMinSize());
    top->Add(scroll, 1, wxEXPAND);

    if (!allowed.empty() || !manual.empty()) {
        auto* note = new wxStaticText(this, wxID_ANY,
            _("Applying will mark settings as dirty and require reslice before printing."));
        note->SetForegroundColour(wxColour(180, 60, 0));
        note->Wrap(wrap);
        top->Add(note, 0, wxLEFT | wxRIGHT | wxTOP, FromDIP(10));
    }

    // Buttons
    auto* btn_sizer = new wxBoxSizer(wxHORIZONTAL);
    auto* skip_btn = new wxButton(this, wxID_NO, _("Skip"));
    btn_sizer->Add(skip_btn, 0, wxRIGHT, 10);
    auto* cancel_btn = new wxButton(this, wxID_CANCEL, _("Cancel"));
    btn_sizer->Add(cancel_btn, 0, wxRIGHT, 10);

    if (!allowed.empty()) {
        auto* apply_btn = new wxButton(this, wxID_YES, _("Apply and Reslice"));
        apply_btn->SetDefault();
        btn_sizer->Add(apply_btn, 0);
        apply_btn->Bind(wxEVT_BUTTON, &PrintPilotResultDialog::on_apply, this);
    }

    top->Add(btn_sizer, 0, wxALIGN_RIGHT | wxALL, FromDIP(12));

    SetSizer(top);
    fit_to_screen(this, parent, 480, 320);
    // After sizing, let the scroll area shrink when the user makes the dialog smaller.
    scroll->SetMinSize(wxSize(-1, FromDIP(80)));

    skip_btn->Bind(wxEVT_BUTTON, &PrintPilotResultDialog::on_skip, this);
    cancel_btn->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { EndModal(wxID_CANCEL); });
    // Closing the review cancels the send; only the Skip button proceeds.
    Bind(wxEVT_CLOSE_WINDOW, [this](wxCloseEvent& e) {
        m_apply_requested = false;
        EndModal(wxID_CANCEL);
        e.Skip(false);
    });
}

void PrintPilotResultDialog::on_apply(wxCommandEvent&)
{
    m_apply_requested = true;
    EndModal(wxID_YES);
}

void PrintPilotResultDialog::on_skip(wxCommandEvent&)
{
    m_apply_requested = false;
    EndModal(wxID_NO);
}

// ===========================================================================
// PrintPilotErrorDialog
// ===========================================================================
PrintPilotErrorDialog::PrintPilotErrorDialog(wxWindow* parent,
                                             const std::string& error,
                                             const std::string& trigger)
    : wxDialog(parent, wxID_ANY, _("PrintPilot: Analysis Error"),
               wxDefaultPosition, wxDefaultSize,
               wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER)
{
    auto* top = new wxBoxSizer(wxVERTICAL);

    auto* err_lbl = new wxStaticText(this, wxID_ANY,
        wxString::FromUTF8(error));
    err_lbl->SetForegroundColour(wxColour(180, 0, 0));
    err_lbl->Wrap(FromDIP(440));
    top->Add(err_lbl, 0, wxALL | wxEXPAND, 14);

    wxString proceed_label = trigger == "send"
        ? _("Send anyway without AI review")
        : _("OK");

    auto* btn_sizer = new wxBoxSizer(wxHORIZONTAL);
    if (trigger == "send") {
        auto* cancel_btn = new wxButton(this, wxID_CANCEL, _("Cancel send"));
        btn_sizer->Add(cancel_btn, 0, wxRIGHT, 10);
    }
    auto* ok_btn = new wxButton(this, wxID_OK, proceed_label);
    ok_btn->SetDefault();
    btn_sizer->Add(ok_btn, 0);
    top->Add(btn_sizer, 0, wxALIGN_RIGHT | wxALL, 12);

    SetSizer(top);
    fit_to_screen(this, parent, 420, 160);
}

// Slice result of the plate being sent (1-based plate_index), for the server's post-slice
// checks and spool accounting: time, per-layer times, filament volume/weight per slot,
// flush and filament changes, time per extrusion role. Empty when not sliced.
static std::string collect_slice_stats(Plater* plater, int plate_index)
{
    if (!plater || plate_index < 1) return {};
    PartPlate* plate = plater->get_partplate_list().get_plate(plate_index - 1);
    if (!plate || !plate->is_slice_result_valid()) return {};
    GCodeProcessorResult* result = plate->get_slice_result();
    if (!result) return {};
    const PrintEstimatedStatistics& ps = result->print_statistics;
    const auto& mode = ps.modes[static_cast<size_t>(PrintEstimatedStatistics::ETimeMode::Normal)];
    nlohmann::json j;
    j["time_s"] = mode.time;
    nlohmann::json layers = nlohmann::json::array();
    for (size_t i = 0; i < mode.layers_times.size() && i < 5000; ++i)
        layers.push_back(std::round(mode.layers_times[i] * 10.f) / 10.f);
    j["layer_times_s"] = layers;
    nlohmann::json filaments = nlohmann::json::array();
    for (const auto& entry : ps.total_volumes_per_extruder) {
        const size_t extruder = entry.first;
        const double volume = entry.second;
        const double density = extruder < result->filament_densities.size() ? result->filament_densities[extruder] : 0.0;
        auto lookup = [&](const std::map<size_t, double>& m) { auto it = m.find(extruder); return it == m.end() ? 0.0 : it->second; };
        filaments.push_back({{"index", int(extruder) + 1},
                             {"volume_mm3", volume},
                             {"model_mm3", lookup(ps.model_volumes_per_extruder)},
                             {"support_mm3", lookup(ps.support_volumes_per_extruder)},
                             {"tower_mm3", lookup(ps.wipe_tower_volumes_per_extruder)},
                             {"flush_mm3", lookup(ps.flush_per_filament)},
                             {"weight_g", density > 0 ? volume * density / 1000.0 : 0.0}});
    }
    j["filaments"] = filaments;
    j["filament_changes"] = ps.total_filament_changes;
    j["flush_changes"] = ps.total_flush_filament_changes;
    nlohmann::json roles = nlohmann::json::object();
    for (const auto& role_time : mode.roles_times)
        roles[ExtrusionEntity::role_to_string(role_time.first)] = role_time.second;
    j["role_times_s"] = roles;
    return j.dump();
}

// ===========================================================================
// run_printpilot_flow
// ===========================================================================
DialogResult run_printpilot_flow(Plater* plater,
                                 const std::string& temp_3mf_path,
                                 const std::string& trigger,
                                 const std::string& objective,
                                 int plate_index)
{
    // 1. Load config
    PrintPilotConfig cfg;
    if (!cfg.load()) {
        BOOST_LOG_TRIVIAL(info) << "PrintPilot: no config, skipping";
        return DialogResult::Cancelled;
    }

    // The service accepts at most 64 MiB decoded; never send a partial ZIP.
    constexpr std::size_t kMaxBytes = 64ULL * 1024 * 1024;
    bool truncated = false;
    std::string b64 = base64_encode_file(temp_3mf_path, kMaxBytes, truncated);
    if (b64.empty()) {
        BOOST_LOG_TRIVIAL(error) << "PrintPilot: failed to read 3MF at " << temp_3mf_path;
        PrintPilotErrorDialog err_dlg(plater, truncated ? "3MF exceeds the 64 MiB analysis limit." : "Could not read exported 3MF file.", trigger);
        int rc = err_dlg.ShowModal();
        return rc == wxID_OK ? DialogResult::Skipped : DialogResult::Cancelled;
    }
    // 3. Send the effective secure config for model context; apply remains allowlisted.
    std::string settings_json;
    double nozzle_diameter = 0;
    {
        auto* pb = wxGetApp().preset_bundle;
        if (!pb) {
            BOOST_LOG_TRIVIAL(error) << "PrintPilot: preset_bundle is null";
            return DialogResult::Cancelled;
        }
        auto cfg_secure = pb->full_config_secure();
        cfg_secure.erase("print_host");
        if (const auto* nozzles = cfg_secure.option<ConfigOptionFloatsNullable>("nozzle_diameter"))
            if (!nozzles->values.empty()) nozzle_diameter = nozzles->values.front();
        nlohmann::json j;
        for (const std::string& key : cfg_secure.keys()) {
            const ConfigOption* opt = cfg_secure.option(key);
            if (opt) {
                try { j[key] = opt->serialize(); }
                catch (const std::exception& ex) {
                    PrintPilotErrorDialog err_dlg(plater, std::string("Could not serialize settings: ") + ex.what(), trigger);
                    return err_dlg.ShowModal() == wxID_OK ? DialogResult::Error : DialogResult::Cancelled;
                }
            }
        }
        settings_json = j.dump();
    }

    // Extract filename from path
    // Prefer the project name over the random temp export name for the dashboard.
    std::string filename = boost::filesystem::path(temp_3mf_path).filename().string();
    if (plater) {
        const std::string project = plater->get_project_name().ToUTF8().data();
        if (!project.empty())
            filename = project + ".3mf";
    }

    // 4. Show progress dialog while request is in flight
    wxWindow* parent_win = wxGetApp().mainframe;
    PrintPilotProgressDialog progress_dlg(parent_win, trigger);

    // Share cancel token with Http worker
    std::shared_ptr<CancelToken> cancel_token;

    // Result storage (written from CallAfter, read after ShowModal returns)
    struct SharedResult { bool ready = false; AnalysisResult result; PrintPilotProgressDialog* dlg = nullptr; };
    auto shared = std::make_shared<SharedResult>();
    shared->dlg = &progress_dlg;

    // Start async request
    // The printer selected in the device tab, so the server can check its AMS and nozzle.
    std::string printer_serial;
    if (auto* dm = wxGetApp().getDeviceManager())
        if (MachineObject* machine = dm->get_selected_machine())
            printer_serial = machine->get_dev_id();
    const std::string slice_stats = collect_slice_stats(plater, plate_index);
    cancel_token = start_analysis(cfg, b64, settings_json, objective, trigger, filename, plate_index, printer_serial, slice_stats,
        [shared](AnalysisResult res) {
            shared->result = std::move(res);
            shared->ready = true;
            if (shared->dlg && !shared->dlg->was_user_cancelled())
                shared->dlg->finish();
        });

    // Block on modal progress dialog
    int progress_ret = progress_dlg.ShowModal();
    shared->dlg = nullptr;
    bool user_cancelled = progress_dlg.was_user_cancelled() || (progress_ret == wxID_CANCEL);

    if (user_cancelled) {
        cancel_token->cancelled = true;
        return DialogResult::Cancelled;
    }

    // 5. Retrieve result (ready because finish() was called before EndModal)
    AnalysisResult result;
    if (!shared->ready) return DialogResult::Cancelled;
    result = shared->result;

    if (!result.success) {
        PrintPilotErrorDialog err_dlg(parent_win, result.error_message, trigger);
        int rc = err_dlg.ShowModal();
        if (rc == wxID_CANCEL)
            return DialogResult::Cancelled;
        return DialogResult::Error;
    }

    // 6. Filter to allowlist. Numeric (v1) and print-preset settings_v2 keys apply to the
    // Process tab; filament keys apply to the Filament tab only while it edits the plate's
    // filament, otherwise they are shown as "set by hand". Anything else aborts, as before.
    std::vector<SettingChange> allowed_changes;
    std::vector<SettingChange> manual_changes;
    std::set<std::string> seen;
    Tab* print_tab = wxGetApp().get_tab(Preset::TYPE_PRINT);
    Tab* filament_tab = wxGetApp().get_tab(Preset::TYPE_FILAMENT);
    if (!print_tab || !print_tab->get_config()) return DialogResult::Cancelled;
    auto filament_tab_edits = [&](int index) {
        auto* bundle = wxGetApp().preset_bundle;
        return filament_tab && filament_tab->get_config() && filament_tab->get_presets() && bundle &&
               index >= 1 && size_t(index) <= bundle->filament_presets.size() &&
               bundle->filament_presets[size_t(index) - 1] == filament_tab->get_presets()->get_edited_preset().name;
    };
    auto reject = [&]() {
        PrintPilotErrorDialog err_dlg(parent_win, "AI suggestions did not match current settings or allowed bounds.", trigger);
        return err_dlg.ShowModal() == wxID_OK ? DialogResult::Error : DialogResult::Cancelled;
    };
    for (const auto& ch : result.changes) {
        if (!seen.insert(ch.key).second) return reject();
        const ExtendedRule* rule = extended_rule(ch.key);
        if (!rule) {
            const ConfigOption* current = print_tab->get_config()->option(ch.key);
            if (ch.scope != "print" || !current ||
                !valid_change(ch.key, ch.old_value, ch.new_value, current->serialize(), nozzle_diameter))
                return reject();
        } else if (rule->scope == 0) {
            const ConfigOption* current = print_tab->get_config()->option(ch.key);
            if (ch.scope != "print" || !current ||
                !valid_extended_change(ch.key, ch.old_value, ch.new_value, current->serialize()))
                return reject();
        } else {
            if (ch.scope != "filament" || ch.filament_index < 1 || !extended_value(*rule, ch.new_value)) return reject();
            if (!filament_tab_edits(ch.filament_index)) {
                manual_changes.push_back(ch);
                continue;
            }
            const ConfigOption* current = filament_tab->get_config()->option(ch.key);
            std::string first;
            size_t count = 0;
            if (!current || !uniform_first(current->serialize(), first, count) ||
                !valid_extended_change(ch.key, ch.old_value, ch.new_value, first))
                return reject();
        }
        if (ch.old_value == ch.new_value) continue;
        allowed_changes.push_back(ch);
    }

    // 7. Show results dialog
    PrintPilotResultDialog result_dlg(parent_win, result, allowed_changes, manual_changes);
    int review_ret = result_dlg.ShowModal();
    if (review_ret == wxID_CANCEL) return DialogResult::Cancelled;

    if (!result_dlg.apply_requested())
        return DialogResult::Skipped;

    // 8. Apply changes via Tab::load_config, re-checking each value against the live config.
    bool any_applied = false;
    if (!allowed_changes.empty()) {
        DynamicPrintConfig print_patch;
        DynamicPrintConfig filament_patch;
        bool print_dirty = false, filament_dirty = false;
        print_patch.apply(*print_tab->get_config());
        if (filament_tab && filament_tab->get_config())
            filament_patch.apply(*filament_tab->get_config());
        try {
            for (const auto& ch : allowed_changes) {
                const ExtendedRule* rule = extended_rule(ch.key);
                if (!rule) {
                    const ConfigOption* current = print_tab->get_config()->option(ch.key);
                    if (!current || !valid_change(ch.key, ch.old_value, ch.new_value, current->serialize(), nozzle_diameter))
                        throw std::runtime_error("Settings changed during AI review");
                    print_patch.set_deserialize_strict(ch.key,
                        ch.key == "sparse_infill_density" ? ch.new_value + "%" : ch.new_value);
                    print_dirty = true;
                } else if (rule->scope == 0) {
                    const ConfigOption* current = print_tab->get_config()->option(ch.key);
                    if (!current || !valid_extended_change(ch.key, ch.old_value, ch.new_value, current->serialize()))
                        throw std::runtime_error("Settings changed during AI review");
                    print_patch.set_deserialize_strict(ch.key, ch.new_value);
                    print_dirty = true;
                } else {
                    const ConfigOption* current = filament_tab_edits(ch.filament_index)
                                                      ? filament_tab->get_config()->option(ch.key) : nullptr;
                    std::string first;
                    size_t count = 0;
                    if (!current || !uniform_first(current->serialize(), first, count) ||
                        !valid_extended_change(ch.key, ch.old_value, ch.new_value, first))
                        throw std::runtime_error("Filament settings changed during AI review");
                    // keep one value per extruder variant
                    std::string value = ch.new_value;
                    for (size_t i = 1; i < count; ++i) value += "," + ch.new_value;
                    filament_patch.set_deserialize_strict(ch.key, value);
                    filament_dirty = true;
                }
            }
            if (print_dirty) print_tab->load_config(print_patch);
            if (filament_dirty) filament_tab->load_config(filament_patch);
            any_applied = print_dirty || filament_dirty;
        } catch (const std::exception& ex) {
            PrintPilotErrorDialog err_dlg(parent_win, ex.what(), trigger);
            return err_dlg.ShowModal() == wxID_OK ? DialogResult::Error : DialogResult::Cancelled;
        }
    }

    // 9. Mark that reslice is needed; schedule background process invalidation.
    if (any_applied && plater)
        plater->schedule_background_process();

    return any_applied ? DialogResult::Applied : DialogResult::Skipped;
}

}}} // namespace Slic3r::GUI::AI
