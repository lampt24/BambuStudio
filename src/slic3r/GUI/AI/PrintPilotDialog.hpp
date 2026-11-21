#pragma once
// PrintPilotDialog.hpp
// Modal dialog that shows AI analysis results, lets the user review changes,
// and applies a numeric allowlist of keys via Tab::load_config().
// After apply, sets a reslice-required flag so the send is cancelled.

#include <string>
#include <vector>
#include <memory>
#include <functional>

#include <wx/dialog.h>
#include <wx/panel.h>
#include <wx/sizer.h>
#include <wx/stattext.h>
#include <wx/scrolwin.h>
#include <wx/checkbox.h>
#include <wx/gauge.h>
#include <wx/timer.h>

#include "PrintPilotClient.hpp"

namespace Slic3r { namespace GUI {

class Plater;

namespace AI {

// Returned by ShowAnalysis when the user dismisses the dialog.
enum class DialogResult {
    Applied,         // user applied changes; caller must require reslice and cancel send
    Skipped,         // user chose not to apply; send may proceed
    Cancelled,       // user cancelled; send must not proceed (also from X button)
    Error,           // analysis failed; dialog showed error; send proceeds only on user confirmation
};

// --- Progress/waiting dialog ---
// Shown while the HTTP request is in flight.  Closing it cancels the request.
class PrintPilotProgressDialog : public wxDialog {
public:
    PrintPilotProgressDialog(wxWindow* parent, const std::string& trigger);
    ~PrintPilotProgressDialog() override = default;

    // Call after the request completes to unblock ShowModal().
    void finish();
    // Call after the request is cancelled externally.
    void cancel_external();

    bool was_user_cancelled() const { return m_user_cancelled; }

private:
    wxGauge* m_gauge = nullptr;
    wxTimer  m_pulse_timer;
    bool     m_user_cancelled = false;
    bool     m_finished = false;

    void on_cancel_btn(wxCommandEvent&);
    void on_timer(wxTimerEvent&);
};

// --- Results dialog ---
// Shows summary + change list; Apply or Skip.
class PrintPilotResultDialog : public wxDialog {
public:
    // changes: filtered to allowlist before display
    PrintPilotResultDialog(wxWindow* parent,
                           const AnalysisResult& result,
                           const std::vector<SettingChange>& allowed_changes);
    ~PrintPilotResultDialog() override = default;

    // After ShowModal: true if user clicked Apply
    bool apply_requested() const { return m_apply_requested; }

private:
    bool m_apply_requested = false;
    void on_apply(wxCommandEvent&);
    void on_skip(wxCommandEvent&);
};

// --- Error dialog ---
class PrintPilotErrorDialog : public wxDialog {
public:
    PrintPilotErrorDialog(wxWindow* parent, const std::string& error,
                          const std::string& trigger);
    ~PrintPilotErrorDialog() override = default;
    // ShowModal returns wxID_OK (proceed) or wxID_CANCEL (abort send).
};

// ---------------------------------------------------------------------------
// High-level orchestrator
// ---------------------------------------------------------------------------
// Runs the full PrintPilot flow:
//   1. Export 3MF to a temp file (caller provides path).
//   2. Show progress dialog while analysing.
//   3. Show results dialog.
//   4. If user applies: mutate Tab configs and schedule_background_process.
//   5. Return DialogResult.
//
// Called from the wx main thread.
// plater must outlive this call (it is modal – fine).
// If DialogResult::Applied is returned the caller MUST abort the send and
// notify the user to reslice.
DialogResult run_printpilot_flow(Plater* plater,
                                 const std::string& temp_3mf_path,
                                 const std::string& trigger,
                                 const std::string& objective = "quality");

}}} // namespace Slic3r::GUI::AI
