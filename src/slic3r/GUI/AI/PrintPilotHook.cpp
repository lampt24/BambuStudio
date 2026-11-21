// PrintPilotHook.cpp
#include "PrintPilotHook.hpp"
#include "PrintPilotConfig.hpp"
#include "PrintPilotDialog.hpp"

#include <string>
#include <cstdlib>

#include <boost/filesystem/path.hpp>
#include <boost/filesystem/operations.hpp>
#include <boost/uuid/uuid.hpp>
#include <boost/uuid/uuid_generators.hpp>
#include <boost/uuid/uuid_io.hpp>
#include <boost/log/trivial.hpp>

#include <wx/msgdlg.h>
#include <wx/choicdlg.h>
#include <wx/file.h>

#include "slic3r/GUI/GUI_App.hpp"
#include "slic3r/GUI/Plater.hpp"
#include "slic3r/GUI/MainFrame.hpp"
#include "slic3r/GUI/PartPlate.hpp"
#include "slic3r/GUI/NotificationManager.hpp"
#include "libslic3r/Format/bbs_3mf.hpp"   // SaveStrategy
#include "libslic3r/Utils.hpp"             // data_dir()

namespace Slic3r { namespace GUI { namespace AI {

thread_local bool redacting_export = false;

namespace {

// Returns a unique temp path under data_dir()/printpilot_tmp/
boost::filesystem::path make_temp_3mf_path()
{
    boost::filesystem::path tmp_dir =
        boost::filesystem::path(Slic3r::data_dir()) / "printpilot_tmp";
    boost::system::error_code ec;
    boost::filesystem::create_directories(tmp_dir, ec);

    auto uuid = boost::uuids::to_string(boost::uuids::random_generator()());
    return tmp_dir / (uuid + ".3mf");
}

// Push a simple text notification safely
void push_info(const std::string& text)
{
    if (auto* nm = wxGetApp().plater() ?
                   wxGetApp().plater()->get_notification_manager() : nullptr)
        nm->push_notification(text);
}

} // anonymous

// ---------------------------------------------------------------------------
// Core: export → analyse → apply/skip
// Returns true (send proceeds) or false (send must be aborted).
// ---------------------------------------------------------------------------
static bool run_flow_internal(Plater* plater, bool isall, const std::string& trigger,
                              const std::string& objective)
{
    if (!plater) return false;

    // Manual review always explains how to enable the feature.
    PrintPilotConfig cfg;
    if (!cfg.load()) {
        const auto path = boost::filesystem::path(Slic3r::data_dir()) / "printpilot.json";
        boost::system::error_code config_error;
        const bool configured = boost::filesystem::exists(path, config_error) || config_error;
        if (trigger == "manual") {
            if (!configured) {
                wxFile file;
                if (file.Create(from_path(path), false)) {
                    file.Write("{\n  \"server_url\": \"http://100.64.0.1:8080\",\n  \"token\": \"\"\n}\n");
                    file.Close();
                }
            }
            wxMessageBox(wxString::Format(_("Set server_url and token in %s, then run PrintPilot again."),
                         from_path(path)),
                         _("PrintPilot setup"), wxOK | wxICON_INFORMATION, wxGetApp().mainframe);
            return false;
        }
        if (!configured) return true;
        return wxMessageBox(_("PrintPilot configuration is invalid or unreadable. Send without AI review?"),
                            _("PrintPilot"), wxYES_NO | wxICON_WARNING, wxGetApp().mainframe) == wxYES;
    }
    if (plater->only_gcode_mode()) {
        if (trigger == "manual") {
            wxMessageBox(_("PrintPilot analysis requires a 3MF project with editable settings."),
                         _("PrintPilot"), wxOK | wxICON_INFORMATION, wxGetApp().mainframe);
            return false;
        }
        return wxMessageBox(_("PrintPilot cannot analyze a G-code-only file. Send without AI review?"),
                            _("PrintPilot"), wxYES_NO | wxICON_WARNING, wxGetApp().mainframe) == wxYES;
    }

    // Export a silent 3MF to temp path.
    // SaveStrategy::Silence suppresses any file dialog.
    // We export the full model (SplitModel for multi-object fidelity).
    boost::filesystem::path tmp_path = make_temp_3mf_path();
    auto strategy = SaveStrategy::Silence | SaveStrategy::SplitModel;

    int plate_idx = isall ? PLATE_ALL_IDX
                          : plater->get_partplate_list().get_curr_plate_index();

    int ret = -1;
    try {
        struct RedactGuard {
            RedactGuard() { redacting_export = true; }
            ~RedactGuard() { redacting_export = false; }
        } guard;
        ret = plater->export_3mf(tmp_path, strategy, plate_idx);
    } catch (const std::exception& ex) {
        BOOST_LOG_TRIVIAL(error) << "PrintPilot: export exception: " << ex.what();
        boost::system::error_code ec;
        boost::filesystem::remove(tmp_path, ec);
        wxMessageBox(_("PrintPilot export failed. The send was cancelled."),
                     _("PrintPilot"), wxOK | wxICON_ERROR, wxGetApp().mainframe);
        return false;
    } catch (...) {
        boost::system::error_code ec;
        boost::filesystem::remove(tmp_path, ec);
        wxMessageBox(_("PrintPilot export failed. The send was cancelled."),
                     _("PrintPilot"), wxOK | wxICON_ERROR, wxGetApp().mainframe);
        return false;
    }
    if (ret < 0) {
        boost::system::error_code ec;
        boost::filesystem::remove(tmp_path, ec);
        BOOST_LOG_TRIVIAL(error)
            << "PrintPilot: export_3mf failed (ret=" << ret << ")";
        if (trigger == "manual") {
            wxMessageBox(_("Could not export the 3MF for analysis."),
                         _("PrintPilot"), wxOK | wxICON_ERROR, wxGetApp().mainframe);
            return false;
        }
        // Offer proceed anyway
        int rc = wxMessageBox(
            _("PrintPilot: Could not export 3MF for analysis.\n"
              "Proceed with send without AI review?"),
            _("PrintPilot"), wxYES_NO | wxICON_WARNING, wxGetApp().mainframe);
        return rc == wxYES;
    }

    // Run the full flow (shows modal dialogs)
    DialogResult flow_result;
    try {
        flow_result = run_printpilot_flow(plater, tmp_path.string(), trigger, objective);
    } catch (const std::exception& ex) {
        BOOST_LOG_TRIVIAL(error) << "PrintPilot: exception in flow: " << ex.what();
        wxMessageBox(_("PrintPilot analysis failed. The send was cancelled."),
                     _("PrintPilot"), wxOK | wxICON_ERROR, wxGetApp().mainframe);
        flow_result = DialogResult::Cancelled;
    } catch (...) {
        wxMessageBox(_("PrintPilot analysis failed. The send was cancelled."),
                     _("PrintPilot"), wxOK | wxICON_ERROR, wxGetApp().mainframe);
        flow_result = DialogResult::Cancelled;
    }

    // Clean up temp file
    boost::system::error_code ec;
    boost::filesystem::remove(tmp_path, ec);

    switch (flow_result) {
        case DialogResult::Applied:
            // Changes were applied.  User must reslice before printing.
            push_info("PrintPilot applied AI settings. Please reslice before printing.");
            wxMessageBox(
                _("PrintPilot applied setting changes.\n\n"
                  "Re-slicing is required before sending to the printer.\n"
                  "The send has been cancelled."),
                _("PrintPilot: Reslice Required"),
                wxOK | wxICON_INFORMATION, wxGetApp().mainframe);
            return false; // abort send

        case DialogResult::Cancelled:
            // User explicitly cancelled → abort send
            return false;

        case DialogResult::Error:
            // Error shown in dialog; user chose to proceed or not.
            // Returning true here: error dialog already offered proceed/cancel choice.
            // If the user clicked "Cancel send" in the error dialog, flow_result
            // would be DialogResult::Cancelled.  So here we know they chose proceed.
            return true;

        case DialogResult::Skipped:
        default:
            return true;
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
bool run_before_send(Plater* plater, bool isall)
{
    return run_flow_internal(plater, isall, "send", "quality");
}

bool run_before_queue_send()
{
    PrintPilotConfig cfg;
    if (!cfg.load()) {
        boost::system::error_code ec;
        if (!boost::filesystem::exists(boost::filesystem::path(Slic3r::data_dir()) / "printpilot.json", ec) && !ec)
            return true;
        return wxMessageBox(_("PrintPilot configuration is invalid or unreadable. Send without AI review?"),
                            _("PrintPilot"), wxYES_NO | wxICON_WARNING, wxGetApp().mainframe) == wxYES;
    }
    return wxMessageBox(_("PrintPilot cannot analyze this queued cloud job's prepared settings. Send without AI review?"),
                        _("PrintPilot"), wxYES_NO | wxICON_WARNING, wxGetApp().mainframe) == wxYES;
}

void run_manual(Plater* plater)
{
    if (!plater) return;
    const wxString choices[] = { _("Quality"), _("Strength"), _("Speed"), _("Economy") };
    wxSingleChoiceDialog choice(wxGetApp().mainframe, _("Choose an analysis objective"),
                                _("PrintPilot"), 4, choices);
    if (choice.ShowModal() != wxID_OK) return;
    const char* ids[] = { "quality", "strength", "speed", "economy" };
    run_flow_internal(plater, false, "manual", ids[choice.GetSelection()]);
}

}}} // namespace Slic3r::GUI::AI
