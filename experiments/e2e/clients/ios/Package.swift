// swift-tools-version:5.9
// 합성 E2E 픽스처: isthmus 조인 검증용 최소 iOS 클라이언트 패키지
import PackageDescription

let package = Package(
    name: "ShopClient",
    platforms: [.macOS(.v12), .iOS(.v15)],
    products: [.library(name: "ShopClient", targets: ["ShopClient"])],
    targets: [.target(name: "ShopClient")]
)
