package com.example.shop

/** 주문 화면 상태 홀더 (Android ViewModel 없이 호출 계층만 표현한 합성 클래스). */
class OrderViewModel(private val repository: OrderRepository = OrderRepository()) {
    /** 마지막으로 받은 주문 응답 */
    var lastOrder: Map<String, Any>? = null
        private set

    /**
     * 주문을 다시 불러온다.
     * @param id 주문 식별자
     */
    fun refresh(id: Int) {
        lastOrder = repository.load(id)
    }
}

/** 결제 화면 상태 홀더. */
class CheckoutViewModel(private val repository: CheckoutRepository = CheckoutRepository()) {
    /** 마지막 결제 응답 */
    var lastReceipt: Map<String, Any>? = null
        private set

    /** 결제 버튼 동작: 결제를 제출한다. */
    fun pay() {
        lastReceipt = repository.submit()
    }
}
