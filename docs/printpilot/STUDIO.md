# PrintPilot in Bambu Studio

Build this fork on macOS or Windows with the normal Bambu Studio build process. The PrintPilot sources are included in `src/slic3r/CMakeLists.txt`; no extra client dependency is needed. Native builds and printer behavior require the CI and hardware checks in `PLAN.md`.

Open **File → PrintPilot AI settings** or the **AI auto setting** button at the top of the Prepare sidebar. On first use, Studio creates `printpilot.json` in its configuration folder. Replace the example `server_url` with the private Tailscale server address and set `token` to the manager token. Restart is unnecessary. Keep the token outside the repository. The menu lets you choose quality, strength, speed, or economy. Sending uses quality by default.

PrintPilot exports the current project as a temporary 3MF, sends it and the effective secure settings to the configured server, then shows the analysis. **Apply and Reslice** updates only bounded numeric print settings and cancels the current send; slice again before sending. **Skip** explicitly continues without applying suggestions. **Cancel**, closing the review, or cancelling the progress dialog stops the send. Analysis errors offer an explicit **Send anyway without AI review** choice. G-code-only files and prepared cloud queue jobs cannot be analyzed; their send entry points offer an explicit choice to continue without review.

The local client accepts only these settings: `layer_height` (0.08–0.28), `initial_layer_print_height` (0.1–0.3), `wall_loops` (1–10), `top_shell_layers` and `bottom_shell_layers` (1–20), `sparse_infill_density` (0–100%), `outer_wall_speed` (10–200), `inner_wall_speed` and `sparse_infill_speed` (10–300), and `brim_width` (0–20). Proposed old values must match the current print preset before any change is applied. Layer heights must stay at or below 80% of the selected nozzle diameter. The client refuses partial or oversized 3MF files above 64 MiB.

Run the standalone local validation check with:

```sh
g++ -std=c++17 src/slic3r/GUI/AI/PrintPilotValidationCheck.cpp -o /tmp/printpilot-validation-check
/tmp/printpilot-validation-check
```

PrintPilot removes print host credentials, including credentials embedded in `print_host` URLs, from the AI settings payload and temporary 3MF config. Project geometry and user-authored metadata are still transmitted to the configured server. PrintPilot does not change Studio's cloud or Handy connection. Printer monitoring remains read-only and is handled by the separate bridge and server.
