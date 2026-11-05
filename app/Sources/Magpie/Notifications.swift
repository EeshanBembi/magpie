import Foundation
import UserNotifications

final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    private static let seenKey = "MagpieSeenNotificationIDs"
    private static let recordAction = "MAGPIE_RECORD_MEETING"
    private static let meetingCategory = "MAGPIE_MEETING_DETECTED"

    private let lock = NSLock()
    private var seen: Set<String>
    private var onRecord: (() -> Void)?
    private var pollTask: Task<Void, Never>?

    override init() {
        let stored = UserDefaults.standard.stringArray(forKey: NotificationManager.seenKey) ?? []
        seen = Set(stored)
        super.init()
    }

    func start(onRecord: @escaping () -> Void) {
        self.onRecord = onRecord

        let center = UNUserNotificationCenter.current()
        center.delegate = self

        let record = UNNotificationAction(
            identifier: NotificationManager.recordAction,
            title: "Record",
            options: []
        )
        let category = UNNotificationCategory(
            identifier: NotificationManager.meetingCategory,
            actions: [record],
            intentIdentifiers: [],
            options: []
        )
        center.setNotificationCategories([category])
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }

        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.poll()
                try? await Task.sleep(nanoseconds: 60_000_000_000)
            }
        }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    private func poll() async {
        var request = URLRequest(url: MagpieAPI.url("/api/notifications"))
        request.timeoutInterval = 10
        request.cachePolicy = .reloadIgnoringLocalCacheData
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let items = root["notifications"] as? [[String: Any]]
        else {
            return
        }

        for item in items {
            guard let id = item["id"] as? String, markSeen(id) else { continue }
            let title = (item["title"] as? String) ?? "Magpie"
            let body = (item["body"] as? String) ?? ""
            let kind = (item["kind"] as? String) ?? ""
            post(id: id, title: title, body: body, kind: kind)
        }
    }

    private func markSeen(_ id: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if seen.contains(id) {
            return false
        }
        seen.insert(id)
        if seen.count > 500 {
            let trimmed = Array(seen).suffix(400)
            seen = Set(trimmed)
        }
        UserDefaults.standard.set(Array(seen), forKey: NotificationManager.seenKey)
        return true
    }

    private func post(id: String, title: String, body: String, kind: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        if kind == "meeting_detected" {
            content.categoryIdentifier = NotificationManager.meetingCategory
        }

        let request = UNNotificationRequest(
            identifier: "magpie-\(id)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }

    // MARK: - UNUserNotificationCenterDelegate

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler:
            @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if response.actionIdentifier == NotificationManager.recordAction {
            let handler = onRecord
            DispatchQueue.main.async {
                handler?()
            }
        }
        completionHandler()
    }
}
