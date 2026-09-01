// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "SlurmboardApp",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/migueldeicaza/SwiftTerm.git", exact: "1.20.0")
    ],
    targets: [
        .executableTarget(
            name: "SlurmboardApp",
            dependencies: ["SwiftTerm"],
            path: "Sources/SlurmboardApp"
        )
    ]
)
