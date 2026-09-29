package com.example.shop

/** 주문 조회 저장소. 화면 계층이 Retrofit 을 직접 알지 않도록 분리한다. */
class OrderRepository(private val service: OrdersService = createOrdersService()) {
    /**
     * 주문 상세를 동기 조회한다.
     * @param id 주문 식별자
     * @return 응답 본문, 실패 시 null
     */
    fun load(id: Int): Map<String, Any>? = service.getOrder(id).execute().body()
}

/** 결제 저장소. */
class CheckoutRepository(private val service: OrdersService = createOrdersService()) {
    /**
     * 결제를 제출한다.
     * @return 응답 본문, 실패 시 null
     */
    fun submit(): Map<String, Any>? = service.checkout(mapOf("confirm" to true)).execute().body()
}
