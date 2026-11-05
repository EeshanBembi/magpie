import AppKit
import Carbon.HIToolbox
import Foundation
import SwiftUI

// MARK: - Streaming model

@MainActor
final class QuickAskModel: ObservableObject {
    @Published var query = ""
    @Published var answer = ""
    @Published var streaming = false
    @Published var error: String?
    @Published var chatID: Int?

    private var task: Task<Void, Never>?

    func reset() {
        task?.cancel()
        task = nil
        query = ""
        answer = ""
        streaming = false
        error = nil
        chatID = nil
    }

    func submit() {
        let message = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { return }

        task?.cancel()
        answer = ""
        error = nil
        chatID = nil
        streaming = true
        task = Task { [weak self] in
            await self?.stream(message: message)
        }
    }

    private func stream(message: String) async {
        do {
            var request = URLRequest(url: MagpieAPI.url("/api/chat"))
            request.httpMethod = "POST"
            request.timeoutInterval = 180
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(
                withJSONObject: ["message": message]
            )

            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw MagpieAPIError.badResponse
            }
            guard (200..<300).contains(http.statusCode) else {
                throw MagpieAPIError.http(http.statusCode, "Magpie could not answer.")
            }

            for try await line in bytes.lines {
                guard line.hasPrefix("data:") else { continue }
                let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                guard !payload.isEmpty,
                      let data = payload.data(using: .utf8),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                else {
                    continue
                }
                if let delta = object["delta"] as? String {
                    answer += delta
                }
                if let message = object["error"] as? String {
                    error = message
                }
                if let done = object["done"] as? Bool, done {
                    if let id = object["chat_id"] as? Int {
                        chatID = id
                    }
                }
            }
        } catch is CancellationError {
            // A new question replaced this one.
        } catch {
            if !Task.isCancelled {
                self.error = error.localizedDescription
            }
        }
        streaming = false
    }
}

// MARK: - Quick Ask panel

@MainActor
final class QuickAskController {
    private let model = QuickAskModel()
    private var panel: QuickAskPanel?
    private var hotKeyRef: EventHotKeyRef?
    private var eventHandlerRef: EventHandlerRef?
    private var onOpenChat: ((Int?) -> Void)?

    func install(onOpenChat: @escaping (Int?) -> Void) {
        self.onOpenChat = onOpenChat
        installHotKey()
    }

    func toggle() {
        if let panel, panel.isVisible {
            close()
        } else {
            show()
        }
    }

    func show() {
        let panel = ensurePanel()
        model.reset()
        panel.contentView = makeContentView()
        position(panel)
        panel.makeKeyAndOrderFront(nil)
        panel.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
    }

    func close() {
        panel?.orderOut(nil)
    }

    func shutdown() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }
        if let eventHandlerRef {
            RemoveEventHandler(eventHandlerRef)
            self.eventHandlerRef = nil
        }
        panel?.orderOut(nil)
        panel = nil
    }

    fileprivate func handleHotKey() {
        toggle()
    }

    private func ensurePanel() -> QuickAskPanel {
        if let panel { return panel }
        let panel = QuickAskPanel(
            contentRect: NSRect(x: 0, y: 0, width: 640, height: 360),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.isReleasedWhenClosed = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.appearance = NSAppearance(named: .darkAqua)
        self.panel = panel
        return panel
    }

    private func makeContentView() -> NSView {
        let effect = NSVisualEffectView()
        effect.material = .hudWindow
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.appearance = NSAppearance(named: .darkAqua)
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 18
        effect.layer?.masksToBounds = true

        let view = QuickAskView(
            model: model,
            onClose: { [weak self] in self?.close() },
            onOpenChat: { [weak self] in self?.openChat() }
        )
        let hosting = NSHostingView(rootView: view)
        hosting.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(hosting)
        NSLayoutConstraint.activate([
            hosting.leadingAnchor.constraint(equalTo: effect.leadingAnchor),
            hosting.trailingAnchor.constraint(equalTo: effect.trailingAnchor),
            hosting.topAnchor.constraint(equalTo: effect.topAnchor),
            hosting.bottomAnchor.constraint(equalTo: effect.bottomAnchor),
        ])
        return effect
    }

    private func position(_ panel: NSPanel) {
        let size = NSSize(width: 640, height: 360)
        panel.setContentSize(size)
        let screen = NSScreen.main ?? NSScreen.screens.first
        if let frame = screen?.visibleFrame {
            let origin = NSPoint(
                x: frame.midX - size.width / 2,
                y: frame.midY - size.height / 2 + frame.height * 0.08
            )
            panel.setFrameOrigin(origin)
        } else {
            panel.center()
        }
    }

    private func openChat() {
        let chatID = model.chatID
        close()
        onOpenChat?(chatID)
    }

    private func installHotKey() {
        guard hotKeyRef == nil else { return }

        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        let userData = Unmanaged.passUnretained(self).toOpaque()
        InstallEventHandler(
            GetApplicationEventTarget(),
            magpieQuickAskHotKeyHandler,
            1,
            &eventType,
            userData,
            &eventHandlerRef
        )

        let hotKeyID = EventHotKeyID(signature: OSType(0x4D47_5051), id: 1)
        RegisterEventHotKey(
            UInt32(kVK_Space),
            UInt32(optionKey),
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )
    }
}

// MARK: - Carbon hotkey plumbing

private func magpieQuickAskHotKeyHandler(
    _ callRef: EventHandlerCallRef?,
    _ event: EventRef?,
    _ userData: UnsafeMutableRawPointer?
) -> OSStatus {
    guard let userData else { return noErr }
    let controller = Unmanaged<QuickAskController>
        .fromOpaque(userData)
        .takeUnretainedValue()
    Task { @MainActor in
        controller.handleHotKey()
    }
    return noErr
}

final class QuickAskPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

// MARK: - SwiftUI content

struct QuickAskView: View {
    @ObservedObject var model: QuickAskModel
    let onClose: () -> Void
    let onOpenChat: () -> Void

    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: "sparkles")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.75))

                TextField("Ask Magpie…", text: $model.query)
                    .textFieldStyle(.plain)
                    .font(.system(size: 20, weight: .medium))
                    .focused($focused)
                    .onSubmit {
                        if model.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                            onOpenChat()
                        } else {
                            model.submit()
                        }
                    }

                if model.streaming {
                    ProgressView()
                        .controlSize(.small)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Color.white.opacity(0.08))
            )

            if model.error != nil || !model.answer.isEmpty {
                ScrollView {
                    Text(model.answer.isEmpty ? (model.error ?? "") : model.answer)
                        .font(.system(size: 15))
                        .foregroundStyle(model.answer.isEmpty ? Color.red : Color.white)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 210)
            } else {
                Text("Ask about your day. Press Enter to ask, or Enter on an empty field to open Magpie.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            Spacer(minLength: 0)

            HStack {
                Text("⌥Space to close")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Esc") {
                    onClose()
                }
                .buttonStyle(.plain)
                .font(.caption)
                .foregroundStyle(.secondary)
                .keyboardShortcut(.escape, modifiers: [])
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color.clear)
        .onExitCommand(perform: onClose)
        .onAppear {
            DispatchQueue.main.async {
                focused = true
            }
        }
    }
}
