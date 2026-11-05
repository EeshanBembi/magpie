import Foundation

enum MagpieBuild {
    static let version = "4.0.0"
}

enum RepoLocation {
    /// The repo path baked in at build time, unless MAGPIE_REPO overrides it.
    static var path: String {
        if let override = ProcessInfo.processInfo.environment["MAGPIE_REPO"],
           !override.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return (override as NSString).expandingTildeInPath
        }
        if !bakedRepoPath.isEmpty {
            return (bakedRepoPath as NSString).expandingTildeInPath
        }
        return FileManager.default.currentDirectoryPath
    }
}

enum MagpieAPIError: LocalizedError {
    case badResponse
    case http(Int, String)

    var errorDescription: String? {
        switch self {
        case .badResponse:
            return "Magpie returned an invalid response."
        case let .http(status, message):
            if message.isEmpty {
                return "Magpie returned HTTP \(status)."
            }
            return message
        }
    }
}

enum MagpieAPI {
    static let host = "127.0.0.1"
    static let port = 7777

    static var baseURL: URL {
        URL(string: "http://\(host):\(port)")!
    }

    static func url(_ path: String) -> URL {
        URL(string: path, relativeTo: baseURL)!
    }

    static func webURL(route: String) -> URL {
        URL(string: "http://\(host):\(port)/#/\(route)")!
    }

    static func isUp() async -> Bool {
        var request = URLRequest(url: url("/api/status"))
        request.timeoutInterval = 1.5
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { return false }
            return (200..<300).contains(http.statusCode)
        } catch {
            return false
        }
    }

    static func getJSON<T: Decodable>(_ type: T.Type, _ path: String, timeout: TimeInterval = 8) async throws -> T {
        var request = URLRequest(url: url(path))
        request.timeoutInterval = timeout
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, response) = try await URLSession.shared.data(for: request)
        try validate(response, data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    static func postJSON<T: Decodable>(
        _ type: T.Type,
        _ path: String,
        body: [String: Any] = [:],
        timeout: TimeInterval = 30
    ) async throws -> T {
        let data = try await post(path, body: body, timeout: timeout)
        return try JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    static func post(
        _ path: String,
        body: [String: Any] = [:],
        timeout: TimeInterval = 30
    ) async throws -> Data {
        var request = URLRequest(url: url(path))
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        try validate(response, data)
        return data
    }

    static func validate(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw MagpieAPIError.badResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            var message = ""
            if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let detail = object["detail"] as? String {
                    message = detail
                } else if let error = object["error"] as? String {
                    message = error
                }
            }
            if message.isEmpty {
                message = String(data: data, encoding: .utf8) ?? ""
            }
            throw MagpieAPIError.http(http.statusCode, message)
        }
    }
}

// MARK: - Response models

struct MagpieStatus: Decodable {
    var capturing: Bool?
    var paused: Bool?
    var running: Bool?
    var meetingDetected: Bool?
    var permissionProblem: String?
    var lastError: String?
    var permission: PermissionInfo?
    var counts: MagpieCounts?

    enum CodingKeys: String, CodingKey {
        case capturing
        case paused
        case running
        case counts
        case permission
        case meetingDetected = "meeting_detected"
        case permissionProblem = "permission_problem"
        case lastError = "last_error"
    }
}

struct PermissionInfo: Decodable {
    var screenRecording: Bool?

    enum CodingKeys: String, CodingKey {
        case screenRecording = "screen_recording"
    }
}

struct MagpieCounts: Decodable {
    var captures: Int?
    var activities: Int?
    var loops: Int?
    var meetings: Int?
}

struct TimelineResponse: Decodable {
    var day: String?
    var activities: [TimelineActivity]?
    var appUsage: [AppUsage]?

    enum CodingKeys: String, CodingKey {
        case day
        case activities
        case appUsage = "app_usage"
    }
}

struct TimelineActivity: Decodable {
    var id: Int?
    var app: String?
    var startTs: Double?
    var endTs: Double?

    enum CodingKeys: String, CodingKey {
        case id
        case app
        case startTs = "start_ts"
        case endTs = "end_ts"
    }
}

struct AppUsage: Decodable, Identifiable {
    var app: String
    var seconds: Double
    var captureCount: Int?

    var id: String { app }

    enum CodingKeys: String, CodingKey {
        case app
        case seconds
        case captureCount = "capture_count"
    }
}

struct MeetingSummary: Decodable {
    var id: Int
    var title: String?
    var status: String?
    var startTs: Double?

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case status
        case startTs = "start_ts"
    }
}

struct MeetingsResponse: Decodable {
    var meetings: [MeetingSummary]?
    var count: Int?
}

struct NotificationRecord {
    var id: String
    var kind: String
    var title: String
    var body: String
}
