FROM eclipse-temurin:17-jdk-jammy AS build

WORKDIR /app

COPY gradlew gradlew.bat settings.gradle build.gradle ./
COPY gradle ./gradle
COPY src ./src

RUN chmod +x gradlew && ./gradlew bootJar copyOtelAgent --no-daemon

FROM eclipse-temurin:17-jre-jammy

WORKDIR /app

RUN apt-get update \
    && apt-get install -y curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /app/build/libs/*.jar /app/app.jar
COPY --from=build /app/build/otel/opentelemetry-javaagent.jar /app/opentelemetry-javaagent.jar
ENV OTEL_SDK_DISABLED=true OTEL_SERVICE_NAME=media-api OTEL_METRICS_EXPORTER=none OTEL_LOGS_EXPORTER=none

EXPOSE 8080

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
