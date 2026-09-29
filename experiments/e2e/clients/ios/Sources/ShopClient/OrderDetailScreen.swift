import Foundation

/// 주문 상세 화면 (UI 프레임워크 없이 호출 계층만 표현한 합성 화면).
public final class OrderDetailScreen {
    /// 화면 상태 뷰모델
    let viewModel: OrderDetailViewModel
    /// 표시 중인 주문 식별자
    let orderId: Int

    /// - Parameters:
    ///   - orderId: 표시할 주문 식별자
    ///   - viewModel: 화면 뷰모델
    public init(orderId: Int, viewModel: OrderDetailViewModel = OrderDetailViewModel()) {
        self.orderId = orderId
        self.viewModel = viewModel
    }

    /// 화면이 나타날 때 주문을 불러온다.
    public func appear() async throws {
        try await viewModel.load(id: orderId)
    }

    /// 취소 버튼을 눌렀을 때 주문을 취소한다.
    public func tapCancel() async throws {
        try await viewModel.cancel(id: orderId)
    }
}
