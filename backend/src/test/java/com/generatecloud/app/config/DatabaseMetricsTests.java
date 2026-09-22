package com.generatecloud.app.config;

import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.sql.SQLException;
import javax.sql.DataSource;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class DatabaseMetricsTests {
    @Test void measuresRealQueriesAndErrorsWithoutSqlOrSecretLabels() {
        var metrics = new SimpleMeterRegistry();
        var source = new DriverManagerDataSource("jdbc:h2:mem:telemetry;DB_CLOSE_DELAY=-1", "sa", "");
        var observed = DatabaseMetricsConfig.instrument(source, () -> metrics);
        var jdbc = new JdbcTemplate(observed);
        assertThat(jdbc.queryForObject("SELECT 42", Integer.class)).isEqualTo(42);
        assertThatThrownBy(() -> jdbc.execute("SELECT secret_column FROM nonexistent_table")).isInstanceOf(RuntimeException.class);
        assertThat(metrics.get("database_query_duration").tag("outcome", "success").timer().count()).isEqualTo(1);
        assertThat(metrics.get("database_query_duration").tag("outcome", "error").timer().count()).isEqualTo(1);
        assertThat(metrics.get("database_query_errors").counter().count()).isEqualTo(1);
        assertThat(metrics.getMeters().toString()).doesNotContain("secret_column", "nonexistent_table");
    }
    @Test void recordsConnectionFailuresWithoutRetrying() throws Exception {
        var metrics = new SimpleMeterRegistry();
        var source = mock(DataSource.class);
        when(source.getConnection()).thenThrow(new SQLException("connection failed"));
        var observed = DatabaseMetricsConfig.instrument(source, () -> metrics);
        assertThatThrownBy(observed::getConnection).isInstanceOf(SQLException.class);
        assertThat(metrics.get("database_connection_failures").counter().count()).isEqualTo(1);
        verify(source, times(1)).getConnection();
    }
}
