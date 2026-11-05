import AppKit
import Combine
import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published private(set) var backendReady = false
    @Published private(set) var backendMessage = "Starting Magpie…"
    @Published private(set) var backendError: String?
    @Published private(set) var status: MagpieStatus?
    @Published private(set) var todayCount = 0
    @Published private(set) var topApps: [AppUsage] = []
    @Published private(set) var isRecording = false
    @Published private(set) var isBusy = false
    @Published private(set) var lastActionError: String?
    @Published private(set) var webRoute = "ask"
    @Published private(set) var webChatID: Int?
    @Published private(set) var webReloadToken = 0

    private let backend = BackendLauncher()
    private let notifications = NotificationManager()
    private let quickAsk = QuickAskController()
    private var mainWindowController: MainWindowController?
    private var statusTask: Task<Void, Never>?
    private var timelineTask: Task<Void, Never>?
    private var started = false
    private var activeMeetingID: Int?

    // MARK: - Lifecycle

    func start() {
        guard !started else { return }
        started = true

        notifications.start { [weak self] in
            Task { @MainActor in
                self?.startMeeting()
            }
        }

        quickAsk.install { [weak self] chatID in
            Task { @MainActor in
                self?.showMainWindow(route: "ask", chatID: chatID)
            }
        }

        Task { [weak self] in
            await self?.boot()
        }
    }

    func shutdown() {
        statusTask?.cancel()
        timelineTask?.cancel()
        statusTask = nil
        timelineTask = nil
        notifications.stop()
        quickAsk.shutdown()
        backend.stop()
    }

    private func boot() async {
        backendMessage = "Starting Magpie…"

        if await MagpieAPI.isUp() {
            backendReady = true
        } else {
            backend.start()
            if let error = backend.lastError {
                backendError = error
                backendMessage = "Could not start Magpie."
                startPolling()
                return
            }

            for _ in 0..<60 {
                if await MagpieAPI.isUp() {
                    backendReady = true
                    backendError = nil
                    break
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }

            if !backendReady {
                backendMessage = "Magpie is not responding on port 7777."
                if backendError == nil {
                    backendError =
                        backend.lastError
                        ?? "The backend did not start. Check \(BackendLauncher.logURL().path)."
                }
            }
        }

        if backendReady {
            backendMessage = "Starting Magpie…"
            await refreshStatus()
            await refreshTimeline()
            await refreshMeetings()
        }
        startPolling()
    }

    private func startPolling() {
        statusTask?.cancel()
        statusTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshStatus()
                await self?.refreshMeetings()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }

        timelineTask?.cancel()
        timelineTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshTimeline()
                try? await Task.sleep(nanoseconds: 15_000_000_000)
            }
        }
    }

    // MARK: - Data refresh

    private func refreshStatus() async {
        do {
            let result = try await MagpieAPI.getJSON(MagpieStatus.self, "/api/status")
            status = result
            if !backendReady {
                backendReady = true
                backendMessage = "Starting Magpie…"
                backendError = nil
            }
        } catch {
            if !backendReady {
                backendMessage = "Starting Magpie…"
            }
        }
    }

    private func refreshTimeline() async {
        do {
            let result = try await MagpieAPI.getJSON(TimelineResponse.self, "/api/timeline")
            todayCount = result.activities?.count ?? 0
            topApps = Array((result.appUsage ?? []).prefix(3))
            if !backendReady {
                backendReady = true
                backendError = nil
            }
        } catch {
            // The menu keeps the last known values while the backend restarts.
        }
    }

    private func refreshMeetings() async {
        do {
            let result = try await MagpieAPI.getJSON(MeetingsResponse.self, "/api/meetings")
            let active = (result.meetings ?? []).first { $0.status == "recording" }
            activeMeetingID = active?.id
            isRecording = active != nil
        } catch {
            // Leave the previous state; the status dot already shows offline.
        }
    }

    // MARK: - Actions

    func togglePause() {
        guard !isBusy else { return }
        let paused = status?.paused ?? false
        isBusy = true
        lastActionError = nil
        Task { [weak self] in
            defer { self?.isBusy = false }
            do {
                try await MagpieAPI.post(paused ? "/api/resume" : "/api/pause")
                await self?.refreshStatus()
            } catch {
                self?.lastActionError = error.localizedDescription
            }
        }
    }

    func startMeeting() {
        guard !isBusy, !isRecording else { return }
        isBusy = true
        lastActionError = nil
        Task { [weak self] in
            defer { self?.isBusy = false }
            do {
                let meeting = try await MagpieAPI.postJSON(
                    MeetingSummary.self,
                    "/api/meetings/start"
                )
                if meeting.status == "error" {
                    self?.lastActionError = "Recording failed to start. Check permissions."
                }
                await self?.refreshMeetings()
                if self?.isRecording == false, self?.lastActionError == nil {
                    self?.lastActionError = "Recording did not start. Check permissions."
                }
            } catch {
                self?.lastActionError = error.localizedDescription
            }
        }
    }

    func stopMeeting() {
        guard !isBusy, let meetingID = activeMeetingID else {
            lastActionError = "No meeting is recording."
            return
        }
        isBusy = true
        lastActionError = nil
        Task { [weak self] in
            defer { self?.isBusy = false }
            do {
                _ = try await MagpieAPI.post(
                    "/api/meetings/\(meetingID)/stop",
                    timeout: 300
                )
                await self?.refreshMeetings()
                await self?.refreshTimeline()
            } catch {
                self?.lastActionError = error.localizedDescription
            }
        }
    }

    func showMainWindow(route: String? = nil, chatID: Int? = nil) {
        webChatID = chatID
        let controller = mainWindowController ?? MainWindowController(model: self)
        mainWindowController = controller
        controller.show(route: route)
    }

    func showQuickAsk() {
        quickAsk.toggle()
    }

    func requestRoute(_ route: String) {
        if webRoute != route {
            webRoute = route
        }
    }

    func reloadWebView() {
        webReloadToken += 1
    }

    // MARK: - Menu presentation

    var isPaused: Bool {
        status?.paused ?? false
    }

    var needsPermission: Bool {
        if status?.permission?.screenRecording == false {
            return true
        }
        return !(status?.permissionProblem ?? "").isEmpty
    }

    var statusLabel: String {
        if needsPermission {
            return "Needs permission"
        }
        if isRecording {
            return "Recording meeting"
        }
        if isPaused {
            return "Paused"
        }
        if status == nil {
            return "Connecting…"
        }
        return "Recording screen"
    }

    var statusDotColor: Color {
        if needsPermission {
            return .orange
        }
        if isRecording {
            return .red
        }
        if isPaused {
            return .yellow
        }
        if status == nil {
            return .gray
        }
        return .green
    }

    var todayLabel: String {
        todayCount == 1 ? "1 capture today" : "\(todayCount) captures today"
    }

    func durationLabel(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        if hours > 0 {
            return "\(hours)h \(minutes)m"
        }
        if minutes > 0 {
            return "\(minutes)m"
        }
        return "\(total)s"
    }
}
