// PrintPilotDialog.cpp
#include "PrintPilotDialog.hpp"
#include "PrintPilotConfig.hpp"
#include "PrintPilotClient.hpp"
#include "PrintPilotValidation.hpp"

#include <algorithm>
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

#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/Plater.hpp"
#include "slic3r/GUI/MainFrame.hpp"
#include "slic3r/GUI/Tab.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "nlohmann/json.hpp"

namespace Slic3r { namespace GUI { namespace AI {

// ===========================================================================
// PrintPilotProgressDialog
// ===========================================================================
PrintPilotProgressDialog::PrintPilotProgressDialog(wxWindow* parent,
                                                   const std::string& trigger)
    : wxDialog(parent, wxID_ANY, _("PrintPilot AI Analysis"),
               wxDefaultPosition, wxSize(400, 160),
               wxDEFAULT_DIALOG_STYLE & ~wxCLOSE_BOX)
    , m_pulse_timer(this)
{
    SetMinSize(wxSize(400, 160));
    auto* top = new wxBoxSizer(wxVERTICAL);

    wxString label = trigger == "send"
        ? _("Analysing settings before send…")
        : _("Analysing settings…");
    auto* lbl = new wxStaticText(this, wxID_ANY, label);
    lbl->SetFont(lbl->GetFont().MakeBold());
    top->Add(lbl, 0, wxALL | wxEXPAND, 12);

    m_gauge = new wxGauge(this, wxID_ANY, 100,
                          wxDefaultPosition, wxSize(-1, 8), wxGA_HORIZONTAL);
    top->Add(m_gauge, 0, wxLEFT | wxRIGHT | wxBOTTOM | wxEXPAND, 12);

        auto* note = new wxStaticText(this, wxID_ANY,
        _("You can cancel while analysis runs."));
    note->SetForegroundColour(wxSystemSettings::GetColour(wxSYS_COLOUR_GRAYTEXT));
    top->Add(note, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);

    auto* cancel_btn = new wxButton(this, wxID_CANCEL, _("Cancel"));
    top->Add(cancel_btn, 0, wxALIGN_CENTER | wxBOTTOM, 12);

    SetSizer(top);
    Layout();
    Centre(wxBOTH);

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
                                               const std::vector<SettingChange>& allowed)
    : wxDialog(parent, wxID_ANY, _("PrintPilot: AI Suggestions"),
               wxDefaultPosition, wxSize(560, 460),
               wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER)
{
    SetMinSize(wxSize(400, 300));
    auto* top = new wxBoxSizer(wxVERTICAL);

    // Summary
    if (!result.summary.empty()) {
        auto* lbl = new wxStaticText(this, wxID_ANY, wxString::FromUTF8(result.summary));
        lbl->Wrap(500);
        top->Add(lbl, 0, wxALL | wxEXPAND, 12);
    }

    // Warnings
    for (auto& w : result.warnings) {
        auto* wlbl = new wxStaticText(this, wxID_ANY,
            wxString("⚠ ") + wxString::FromUTF8(w));
        wlbl->SetForegroundColour(wxColour(200, 120, 0));
        wlbl->Wrap(500);
        top->Add(wlbl, 0, wxLEFT | wxRIGHT | wxBOTTOM | wxEXPAND, 10);
    }

    if (allowed.empty()) {
        auto* none = new wxStaticText(this, wxID_ANY,
            _("No applicable numeric settings changes were proposed."));
        none->SetForegroundColour(wxSystemSettings::GetColour(wxSYS_COLOUR_GRAYTEXT));
        top->Add(none, 0, wxALL, 12);
    } else {
        auto* hdr = new wxStaticText(this, wxID_ANY,
            _("Proposed changes (numeric allowlist only):"));
        hdr->SetFont(hdr->GetFont().MakeBold());
        top->Add(hdr, 0, wxLEFT | wxRIGHT | wxTOP, 12);

        auto* scroll = new wxScrolledWindow(this, wxID_ANY,
                           wxDefaultPosition, wxSize(-1, 220));
        scroll->SetScrollRate(0, 10);
        auto* sv = new wxBoxSizer(wxVERTICAL);

        for (auto& ch : allowed) {
            wxString row = wxString::Format(
                "  %s:  %s  →  %s",
                wxString::FromUTF8(ch.key),
                wxString::FromUTF8(ch.old_value),
                wxString::FromUTF8(ch.new_value));
            auto* row_lbl = new wxStaticText(scroll, wxID_ANY, row);
            sv->Add(row_lbl, 0, wxLEFT | wxRIGHT | wxTOP, 6);

            if (!ch.reason.empty()) {
                auto* reason_lbl = new wxStaticText(scroll, wxID_ANY,
                    wxString("    ") + wxString::FromUTF8(ch.reason));
                reason_lbl->SetForegroundColour(
                    wxSystemSettings::GetColour(wxSYS_COLOUR_GRAYTEXT));
                reason_lbl->Wrap(480);
                sv->Add(reason_lbl, 0, wxLEFT | wxBOTTOM, 20);
            }
        }
        scroll->SetSizer(sv);
        scroll->FitInside();
        top->Add(scroll, 1, wxLEFT | wxRIGHT | wxEXPAND, 10);

        auto* note = new wxStaticText(this, wxID_ANY,
            _("Applying will mark settings as dirty and require reslice before printing."));
        note->SetForegroundColour(wxColour(180, 60, 0));
        note->Wrap(500);
        top->Add(note, 0, wxALL, 10);
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

    top->Add(btn_sizer, 0, wxALIGN_RIGHT | wxALL, 12);

    SetSizer(top);
    Layout();
    Centre(wxBOTH);

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
               wxDefaultPosition, wxSize(440, 220),
               wxDEFAULT_DIALOG_STYLE)
{
    auto* top = new wxBoxSizer(wxVERTICAL);

    auto* err_lbl = new wxStaticText(this, wxID_ANY,
        wxString::FromUTF8(error));
    err_lbl->SetForegroundColour(wxColour(180, 0, 0));
    err_lbl->Wrap(400);
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
    Layout();
    Centre(wxBOTH);
}

// ===========================================================================
// run_printpilot_flow
// ===========================================================================
DialogResult run_printpilot_flow(Plater* plater,
                                 const std::string& temp_3mf_path,
                                 const std::string& trigger,
                                 const std::string& objective)
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
    std::string filename = boost::filesystem::path(temp_3mf_path).filename().string();

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
    cancel_token = start_analysis(cfg, b64, settings_json, objective, trigger, filename,
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

    // 6. Filter to allowlist
    std::vector<SettingChange> allowed_changes;
    std::set<std::string> seen;
    Tab* print_tab = wxGetApp().get_tab(Preset::TYPE_PRINT);
    if (!print_tab || !print_tab->get_config()) return DialogResult::Cancelled;
    for (const auto& ch : result.changes) {
        const ConfigOption* current = print_tab->get_config()->option(ch.key);
        if (!current || !seen.insert(ch.key).second ||
            !valid_change(ch.key, ch.old_value, ch.new_value, current->serialize(), nozzle_diameter)) {
            PrintPilotErrorDialog err_dlg(parent_win, "AI suggestions did not match current settings or allowed bounds.", trigger);
            return err_dlg.ShowModal() == wxID_OK ? DialogResult::Error : DialogResult::Cancelled;
        }
        if (ch.old_value == ch.new_value) continue;
        allowed_changes.push_back(ch);
    }

    // 7. Show results dialog
    PrintPilotResultDialog result_dlg(parent_win, result, allowed_changes);
    int review_ret = result_dlg.ShowModal();
    if (review_ret == wxID_CANCEL) return DialogResult::Cancelled;

    if (!result_dlg.apply_requested())
        return DialogResult::Skipped;

    // 8. Apply changes via Tab::load_config
    // Build a DynamicPrintConfig with only the allowed changed keys.
    // We use set_deserialize_strict to parse numeric strings safely.
    bool any_applied = false;
    if (!allowed_changes.empty()) {
        if (print_tab) {
            DynamicPrintConfig patch;
            // Apply to a copy of the current config to get the right option types
            patch.apply(*print_tab->get_config());

            try {
                for (const auto& ch : allowed_changes) {
                    const ConfigOption* current = print_tab->get_config()->option(ch.key);
                    if (!current || !valid_change(ch.key, ch.old_value, ch.new_value, current->serialize(), nozzle_diameter))
                        throw std::runtime_error("Settings changed during AI review");
                    patch.set_deserialize_strict(ch.key,
                        ch.key == "sparse_infill_density" ? ch.new_value + "%" : ch.new_value);
                }
                print_tab->load_config(patch);
                any_applied = true;
            } catch (const std::exception& ex) {
                PrintPilotErrorDialog err_dlg(parent_win, ex.what(), trigger);
                return err_dlg.ShowModal() == wxID_OK ? DialogResult::Error : DialogResult::Cancelled;
            }
        }
    }

    // 9. Mark that reslice is needed; schedule background process invalidation.
    if (any_applied && plater)
        plater->schedule_background_process();

    return any_applied ? DialogResult::Applied : DialogResult::Skipped;
}

}}} // namespace Slic3r::GUI::AI
