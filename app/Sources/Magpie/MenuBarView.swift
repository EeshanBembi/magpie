import AppKit
import SwiftUI

struct MenuBarView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                Circle()
                    .fill(model.statusDotColor)
                    .frame(width: 10, height: 10)
                    .padding(.top, 4)

                VStack(alignment: .leading, spacing: 3) {
                    Text(model.statusLabel)
                        .font(.headline)
                    Text(model.todayLabel)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }

                Spacer(minLength: 0)

                if model.isBusy {
                    ProgressView()
                        .controlSize(.small)
                }
            }

            if !model.backendReady {
                Text(model.backendMessage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if !model.topApps.isEmpty {
                Divider()
                Text("Top apps today")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                ForEach(model.topApps) { usage in
                    HStack {
                        Text(usage.app)
                            .lineLimit(1)
                        Spacer()
                        Text(model.durationLabel(usage.seconds))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    .font(.callout)
                }
            }

            if let error = model.lastActionError ?? model.backendError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider()

            Button(model.isPaused ? "Resume" : "Pause") {
                model.togglePause()
            }
            .disabled(!model.backendReady || model.isBusy)

            if model.isRecording {
                Button("Stop meeting") {
                    model.stopMeeting()
                }
                .disabled(model.isBusy)
            } else {
                Button("Record meeting") {
                    model.startMeeting()
                }
                .disabled(!model.backendReady || model.isBusy)
            }

            Button("Open Magpie") {
                model.showMainWindow()
            }

            Button("Quick Ask (⌥Space)") {
                model.showQuickAsk()
            }

            Button("Settings") {
                model.showMainWindow(route: "settings")
            }

            Divider()

            Button("Quit Magpie") {
                NSApplication.shared.terminate(nil)
            }
            .keyboardShortcut("q")
        }
        .padding(12)
        .frame(width: 292)
    }
}
