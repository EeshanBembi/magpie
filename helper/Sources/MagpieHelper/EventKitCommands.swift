import EventKit
import Foundation

// MARK: - Date helpers

private let isoFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    return formatter
}()

private let isoFormatterFractional: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter
}()

func parseISODate(_ text: String) -> Date? {
    if let date = isoFormatterFractional.date(from: text) {
        return date
    }
    if let date = isoFormatter.date(from: text) {
        return date
    }
    let plain = DateFormatter()
    plain.locale = Locale(identifier: "en_US_POSIX")
    plain.timeZone = TimeZone.current
    for format in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd"] {
        plain.dateFormat = format
        if let date = plain.date(from: text) {
            return date
        }
    }
    return nil
}

func requireISODate(_ text: String, _ label: String) -> Date {
    guard let date = parseISODate(text) else {
        fail("invalid \(label) date: \(text)")
    }
    return date
}

func isoString(_ date: Date) -> String {
    isoFormatter.string(from: date)
}

// MARK: - Flag parsing

func parseFlags(_ arguments: [String], startIndex: Int, allowed: Set<String>) -> [String: String] {
    var result: [String: String] = [:]
    var index = startIndex
    while index < arguments.count {
        let flag = arguments[index]
        guard allowed.contains(flag) else {
            fail("unknown argument: \(flag)")
        }
        guard index + 1 < arguments.count else {
            fail("missing value for \(flag)")
        }
        result[flag] = arguments[index + 1]
        index += 2
    }
    return result
}

// MARK: - calendar-events

func calendarEvents(arguments: [String]) async {
    guard arguments.count >= 4 else {
        fail("usage: magpie-helper calendar-events <start-iso> <end-iso>")
    }
    let start = requireISODate(arguments[2], "start")
    let end = requireISODate(arguments[3], "end")
    guard end >= start else {
        fail("end must not be before start")
    }

    let store = EKEventStore()
    let granted: Bool
    do {
        granted = try await store.requestFullAccessToEvents()
    } catch {
        fail("calendar access error: \(error.localizedDescription)")
    }
    guard granted else {
        fail("calendar access denied")
    }

    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    let events = store.events(matching: predicate).sorted {
        ($0.startDate ?? .distantPast) < ($1.startDate ?? .distantPast)
    }
    let payload: [[String: Any]] = events.map { event in
        [
            "id": event.eventIdentifier ?? "",
            "title": event.title ?? "",
            "start": event.startDate.map(isoString) as Any? ?? NSNull(),
            "end": event.endDate.map(isoString) ?? NSNull(),
            "calendar": event.calendar.title,
            "all_day": event.isAllDay,
            "location": event.location ?? "",
            "notes": event.notes ?? "",
        ]
    }
    emit(["count": events.count, "events": payload])
    exit(0)
}

// MARK: - add-reminder

func addReminder(arguments: [String]) async {
    guard arguments.count >= 3 else {
        fail("usage: magpie-helper add-reminder <title> [--due iso] [--notes text]")
    }
    let title = arguments[2]
    let flags = parseFlags(arguments, startIndex: 3, allowed: ["--due", "--notes"])

    let store = EKEventStore()
    let granted: Bool
    do {
        granted = try await store.requestFullAccessToReminders()
    } catch {
        fail("reminders access error: \(error.localizedDescription)")
    }
    guard granted else {
        fail("reminders access denied")
    }

    let reminder = EKReminder(eventStore: store)
    reminder.title = title
    if let notes = flags["--notes"] {
        reminder.notes = notes
    }
    if let dueText = flags["--due"] {
        let due = requireISODate(dueText, "due")
        reminder.dueDateComponents = Calendar.current.dateComponents(
            [.year, .month, .day, .hour, .minute, .second, .timeZone],
            from: due
        )
    }
    reminder.calendar = store.defaultCalendarForNewReminders()

    do {
        try store.save(reminder, commit: true)
    } catch {
        fail("could not save reminder: \(error.localizedDescription)")
    }
    emit([
        "id": reminder.calendarItemIdentifier,
        "ok": true,
        "title": reminder.title ?? title,
    ])
    exit(0)
}

// MARK: - add-event

func addEvent(arguments: [String]) async {
    guard arguments.count >= 5 else {
        fail("usage: magpie-helper add-event <title> <start-iso> <end-iso> [--notes text]")
    }
    let title = arguments[2]
    let start = requireISODate(arguments[3], "start")
    let end = requireISODate(arguments[4], "end")
    guard end >= start else {
        fail("end must not be before start")
    }
    let flags = parseFlags(arguments, startIndex: 5, allowed: ["--notes"])

    let store = EKEventStore()
    let granted: Bool
    do {
        granted = try await store.requestFullAccessToEvents()
    } catch {
        fail("calendar access error: \(error.localizedDescription)")
    }
    guard granted else {
        fail("calendar access denied")
    }

    let event = EKEvent(eventStore: store)
    event.title = title
    event.startDate = start
    event.endDate = end
    if let notes = flags["--notes"] {
        event.notes = notes
    }
    event.calendar = store.defaultCalendarForNewEvents

    do {
        try store.save(event, span: .thisEvent, commit: true)
    } catch {
        fail("could not save event: \(error.localizedDescription)")
    }
    emit([
        "end": isoString(end),
        "id": event.eventIdentifier ?? "",
        "ok": true,
        "start": isoString(start),
        "title": event.title ?? title,
    ])
    exit(0)
}
