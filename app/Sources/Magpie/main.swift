import AppKit
import Foundation

if CommandLine.arguments.contains("--self-test") {
    let version =
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        ?? MagpieBuild.version
    let payload: [String: Any] = ["ok": true, "version": version]
    if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        print("{\"ok\":true,\"version\":\"\(version)\"}")
    }
    exit(0)
}

MagpieApp.main()
