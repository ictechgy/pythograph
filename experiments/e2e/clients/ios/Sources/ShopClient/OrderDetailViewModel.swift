import Foundation

/// 주문 상세 화면의 상태를 관리한다. API 호출을 화면에서 분리하기 위한 중간 계층이다.
public final class OrderDetailViewModel {
    /// 주문 API 의존성
    let ordersAPI: OrdersAPI
    /// 마지막으로 받은 주문 응답
    public private(set) var lastPayload: Data?

    /// - Parameter ordersAPI: 주문 API 클라이언트
    public init(ordersAPI: OrdersAPI = OrdersAPI()) {
        self.ordersAPI = ordersAPI
    }

    /// 주문 상세를 불러와 상태에 반영한다.
    /// - Parameter id: 주문 식별자
    public func load(id: Int) async throws {
        lastPayload = try await ordersAPI.fetchOrder(id: id)
    }

    /// 주문을 취소하고 결과를 상태에 반영한다.
    /// - Parameter id: 주문 식별자
    public func cancel(id: Int) async throws {
        lastPayload = try await ordersAPI.cancelOrder(id: id)
    }
}
