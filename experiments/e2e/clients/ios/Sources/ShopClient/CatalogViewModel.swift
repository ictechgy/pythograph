import Foundation

/// 상품 카탈로그 화면의 상태를 관리한다.
public final class CatalogViewModel {
    /// 상품 API 의존성
    let productsAPI: ProductsAPI
    /// 마지막으로 받은 상품 목록 응답
    public private(set) var lastPayload: Data?

    /// - Parameter productsAPI: 상품 API 클라이언트
    public init(productsAPI: ProductsAPI = ProductsAPI()) {
        self.productsAPI = productsAPI
    }

    /// 상품 목록을 새로 불러온다.
    public func refresh() async throws {
        lastPayload = try await productsAPI.list()
    }
}
