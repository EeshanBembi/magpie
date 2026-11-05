// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Magpie",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "Magpie", targets: ["Magpie"])
    ],
    targets: [
        .executableTarget(
            name: "Magpie",
            path: "Sources/Magpie",
            swiftSettings: [
                .swiftLanguageMode(.v5)
            ],
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("Carbon"),
                .linkedFramework("UserNotifications"),
                .linkedFramework("WebKit")
            ]
        )
    ]
)
