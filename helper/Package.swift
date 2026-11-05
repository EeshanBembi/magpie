// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "magpie-helper",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "magpie-helper", targets: ["MagpieHelper"])
    ],
    targets: [
        .executableTarget(
            name: "MagpieHelper",
            path: "Sources/MagpieHelper",
            swiftSettings: [
                .swiftLanguageMode(.v5)
            ]
        )
    ]
)
