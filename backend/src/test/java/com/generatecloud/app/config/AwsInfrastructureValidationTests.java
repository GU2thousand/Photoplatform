package com.generatecloud.app.config;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.mock.env.MockEnvironment;
import static org.assertj.core.api.Assertions.*;

class AwsInfrastructureValidationTests {
    private MockEnvironment valid() {
        return new MockEnvironment().withProperty("app.storage.provider", "aws")
                .withProperty("spring.rabbitmq.ssl.enabled", "true")
                .withProperty("spring.rabbitmq.ssl.validate-server-certificate", "true")
                .withProperty("spring.rabbitmq.ssl.verify-hostname", "true")
                .withProperty("spring.datasource.hikari.data-source-properties.sslmode", "verify-full")
                .withProperty("app.seed.enabled", "false")
                .withProperty("app.jwt.secret", "test-generated-unique-non-demo-secret-0123456789")
                .withProperty("spring.datasource.url", "jdbc:postgresql://private-rds.example:5432/photoplatform");
    }
    @Test void acceptsVerifiedTlsAndRegionalStorage() { assertThatCode(() -> AwsInfrastructureValidation.validate(valid())).doesNotThrowAnyException(); }
    @ParameterizedTest
    @ValueSource(strings = {"sslmode=require", "sslmode=disable", "ssl=false", "sslfactory=org.postgresql.ssl.NonValidatingFactory", "sslhostnameverifier=untrusted.CustomVerifier", "ssl%6dode=prefer"})
    void rejectsJdbcUrlOverridesThatDowngradeVerification(String query) {
        assertThatThrownBy(() -> AwsInfrastructureValidation.validate(valid().withProperty("spring.datasource.url",
                "jdbc:postgresql://private-rds.example:5432/db?" + query))).isInstanceOf(IllegalStateException.class);
    }
    @ParameterizedTest
    @ValueSource(strings = {"spring.rabbitmq.ssl.enabled", "spring.rabbitmq.ssl.validate-server-certificate", "spring.rabbitmq.ssl.verify-hostname"})
    void rejectsBrokerTlsDowngrades(String property) {
        assertThatThrownBy(() -> AwsInfrastructureValidation.validate(valid().withProperty(property, "false"))).isInstanceOf(IllegalStateException.class);
    }
    @ParameterizedTest
    @ValueSource(strings = {"short", "local-development-secret-key-change-me", "generate-cloud-demo-secret-key-for-local-development-please-change"})
    void rejectsKnownDemoJwtSecrets(String secret) {
        assertThatThrownBy(() -> AwsInfrastructureValidation.validate(valid().withProperty("app.jwt.secret", secret)))
                .isInstanceOf(IllegalStateException.class).hasMessageNotContaining(secret);
    }
    @Test void rejectsAccidentalLocalProvider() {
        assertThatThrownBy(() -> AwsInfrastructureValidation.validate(valid().withProperty("app.storage.provider", "minio")))
                .isInstanceOf(IllegalStateException.class);
    }
    @Test void dedicatedMigrationConnectionRequiresItsOwnTlsConfiguration() {
        assertThatThrownBy(() -> AwsInfrastructureValidation.validate(valid().withProperty("spring.flyway.user", "migration_user")))
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("Flyway");
        assertThatCode(() -> AwsInfrastructureValidation.validate(valid().withProperty("spring.flyway.user", "migration_user")
                .withProperty("spring.flyway.url", "jdbc:postgresql://private-rds.example/db?sslmode=verify-full&sslrootcert=/app/certs/global-bundle.pem")))
                .doesNotThrowAnyException();
    }
}
