package com.generatecloud.app;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.beans.factory.annotation.Autowired;
import io.micrometer.core.instrument.MeterRegistry;
import javax.sql.DataSource;
import com.zaxxer.hikari.HikariDataSource;
import org.springframework.jdbc.core.JdbcTemplate;
import static org.assertj.core.api.Assertions.*;

@SpringBootTest
class GenerateCloudBackendApplicationTests {
	@Autowired DataSource source;
	@Autowired MeterRegistry metrics;

	@Test
	void contextLoads() {
	}

	@Test
	void queryInstrumentationPreservesPoolAccessAndMetrics() throws Exception {
		assertThat(source.unwrap(HikariDataSource.class)).isNotNull();
		assertThat(new JdbcTemplate(source).queryForObject("SELECT 42", Integer.class)).isEqualTo(42);
		assertThat(metrics.get("database_query_duration").tag("outcome", "success").timer().count()).isGreaterThan(0);
		assertThat(metrics.find("jdbc.connections.active").gauge()).isNotNull();
		assertThat(metrics.find("hikaricp.connections.acquire").timer()).isNotNull();
	}

}
