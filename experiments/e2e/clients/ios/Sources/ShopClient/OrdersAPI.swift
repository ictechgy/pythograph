import Foundation

/// 주문 API 클라이언트. 서버의 주문 상세/취소 라우트를 URLSession 으로 직접 호출한다.
/// 경로 끝의 슬래시는 Django 의 엄격한 trailing slash 규칙 때문에 반드시 유지한다.
public final class OrdersAPI {
    /// 요청에 사용할 세션. 테스트 대체를 위해 주입 가능하게 둔다.
    let session: URLSession

    /// - Parameter session: 요청에 사용할 URLSession (기본값 shared)
    public init(session: URLSession = .shared) {
        self.session = session
    }

    /// 주문 상세를 조회한다 (GET /api/orders/{id}/).
    /// - Parameter id: 주문 식별자
    /// - Returns: 응답 본문 바이트
    public func fetchOrder(id: Int) async throws -> Data {
        let request = URLRequest(url: URL(string: "https://api.example.com/api/orders/\(id)/")!)
        let (data, _) = try await session.data(for: request)
        return data
    }

    /// 주문을 취소한다 (POST /api/orders/{id}/cancel/).
    /// - Parameter id: 취소할 주문 식별자
    /// - Returns: 응답 본문 바이트
    public func cancelOrder(id: Int) async throws -> Data {
        var request = URLRequest(url: URL(string: "https://api.example.com/api/orders/\(id)/cancel/")!)
        request.httpMethod = "POST"
        let (data, _) = try await session.data(for: request)
        return data
    }
}
