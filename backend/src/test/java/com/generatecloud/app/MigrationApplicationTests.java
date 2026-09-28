package com.generatecloud.app;

import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import static org.assertj.core.api.Assertions.*;

class MigrationApplicationTests {
    @Test void missingDedicatedCredentialsFailsBeforeOpeningAnyDatabase() {
        assertThatThrownBy(() -> MigrationApplication.migrate(Map.of()))
                .isInstanceOf(IllegalStateException.class).hasMessage("Missing MIGRATOR_DATABASE_URL");
    }
    @Test void eksCannotSkipAwsTlsGuard() {
        assertThatThrownBy(() -> MigrationApplication.validateConfiguration(Map.of("SPRING_PROFILES_ACTIVE", "eks"),
                "jdbc:postgresql://host/db")).hasMessageContaining("both aws,eks");
    }
    @ParameterizedTest @ValueSource(strings={"sslmode=require", "sslmode=verify-full", "sslmode=verify-full&sslrootcert=/tmp/cert&sslfactory=evil", "sslmode=verify-full&sslrootcert=/tmp/cert&ssl=false", "SSLMODE=verify-full&SSLROOTCERT=/tmp/cert", "ssl%6dode=verify-full&sslrootcert=/tmp/cert", "sslmode=require&sslmode=verify-full&sslrootcert=/tmp/cert"})
    void awsDedicatedConnectionRefusesMissingCertOrTlsDowngrades(String query) {
        assertThatThrownBy(() -> MigrationApplication.validateConfiguration(Map.of("SPRING_PROFILES_ACTIVE", "aws,eks"),
                "jdbc:postgresql://host/db?"+query)).isInstanceOf(IllegalStateException.class);
    }
    @Test void acceptsVerifiedDedicatedConnection() {
        assertThatCode(() -> MigrationApplication.validateConfiguration(Map.of("SPRING_PROFILES_ACTIVE", "aws,eks"),
                "jdbc:postgresql://host/db?sslmode=verify-full&sslrootcert=/app/certs/global-bundle.pem")).doesNotThrowAnyException();
    }
}
