package com.example.shop

import java.net.URL

/**
 * 상품 목록 클라이언트. java.net.URL 로 직접 요청한다.
 * Retrofit 인터페이스 메서드와 달리 본문이 있는 메서드라 kartograph 가 JVM 식별자(usr)를 붙일 수 있다.
 */
class ProductsClient {
    /**
     * 상품 목록을 조회한다 (GET /api/products/).
     * @return 응답 본문 문자열
     */
    fun list(): String {
        return URL("https://api.example.com/api/products/").readText()
    }
}

/** 상품 카탈로그 화면 상태 홀더. */
class CatalogViewModel(private val client: ProductsClient = ProductsClient()) {
    /** 마지막으로 받은 상품 목록 응답 */
    var lastPayload: String? = null
        private set

    /** 상품 목록을 새로 불러온다. */
    fun refresh() {
        lastPayload = client.list()
    }
}
