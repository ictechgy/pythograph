import Foundation

/// 상품 목록 API 클라이언트 (GET /api/products/).
public final class ProductsAPI {
    /// 요청에 사용할 세션
    let session: URLSession

    /// - Parameter session: 요청에 사용할 URLSession (기본값 shared)
    public init(session: URLSession = .shared) {
        self.session = session
    }

    /// 상품 목록을 조회한다.
    /// - Returns: 응답 본문 바이트
    public func list() async throws -> Data {
        let request = URLRequest(url: URL(string: "https://api.example.com/api/products/")!)
        let (data, _) = try await session.data(for: request)
        return data
    }
}
