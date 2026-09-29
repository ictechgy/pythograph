package com.example.shop

import retrofit2.Call
import retrofit2.Retrofit
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path

/** Retrofit 서비스가 붙을 API 베이스 URL. Django 라우트가 host 루트에 있으므로 경로 없이 둔다. */
const val API_BASE_URL = "https://api.example.com/"

/**
 * 주문/결제 API 의 Retrofit 서비스 정의.
 * 경로는 base 상대(선행 슬래시 없음)이며 Django 의 엄격한 trailing slash 때문에 끝 슬래시를 유지한다.
 */
interface OrdersService {
    /** 주문 상세 조회 (GET /api/orders/{id}/). */
    @GET("api/orders/{id}/")
    fun getOrder(@Path("id") id: Int): Call<Map<String, Any>>

    /** 결제 요청 (POST /api/checkout/). */
    @POST("api/checkout/")
    fun checkout(@Body payload: Map<String, Any>): Call<Map<String, Any>>
}

/** 베이스 URL 이 고정된 Retrofit 인스턴스로 [OrdersService] 를 만든다. */
fun createOrdersService(): OrdersService =
    Retrofit.Builder()
        .baseUrl(API_BASE_URL)
        .build()
        .create(OrdersService::class.java)
