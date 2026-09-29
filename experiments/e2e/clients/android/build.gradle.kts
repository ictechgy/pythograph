plugins {
    kotlin("jvm") version "2.4.20"
}

kotlin { jvmToolchain(21) }

dependencies {
    // Retrofit 서비스 인터페이스를 컴파일하기 위한 최소 의존성
    implementation("com.squareup.retrofit2:retrofit:2.12.0")
}
