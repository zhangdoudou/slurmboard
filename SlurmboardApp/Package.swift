// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "SlurmboardApp",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "SlurmboardApp",
            path: "Sources/SlurmboardApp"
        )
    ]
)
