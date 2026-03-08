package com.generatecloud.app.config;

import java.net.URI;
import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.env.EnvironmentPostProcessor;
import org.springframework.core.Ordered;
import org.springframework.core.env.ConfigurableEnvironment;
import org.springframework.core.env.MapPropertySource;
import org.springframework.util.StringUtils;

public class DatabaseUrlEnvironmentPostProcessor implements EnvironmentPostProcessor, Ordered {

    @Override
    public void postProcessEnvironment(ConfigurableEnvironment environment, SpringApplication application) {
        String explicitDatasourceUrl = environment.getProperty("SPRING_DATASOURCE_URL");
        String databaseUrl = environment.getProperty("DATABASE_URL");
        if (!StringUtils.hasText(databaseUrl)) {
            return;
        }

        if (StringUtils.hasText(explicitDatasourceUrl)) {
            return;
        }

        if (databaseUrl.startsWith("jdbc:")) {
            environment.getPropertySources().addFirst(new MapPropertySource(
                    "databaseUrlOverrides",
                    Map.of("spring.datasource.url", databaseUrl)
            ));
            return;
        }

        URI uri = URI.create(databaseUrl);
        String scheme = uri.getScheme();
        if (!"postgres".equalsIgnoreCase(scheme) && !"postgresql".equalsIgnoreCase(scheme)) {
            return;
        }

        String jdbcUrl = "jdbc:postgresql://" + uri.getHost();
        if (uri.getPort() > 0) {
            jdbcUrl += ":" + uri.getPort();
        }
        jdbcUrl += uri.getPath();
        if (StringUtils.hasText(uri.getQuery())) {
            jdbcUrl += "?" + uri.getQuery();
        }

        Map<String, Object> overrides = new LinkedHashMap<>();
        overrides.put("spring.datasource.url", jdbcUrl);

        String userInfo = uri.getUserInfo();
        if (StringUtils.hasText(userInfo)) {
            String[] credentials = userInfo.split(":", 2);
            if (credentials.length >= 1 && !StringUtils.hasText(environment.getProperty("SPRING_DATASOURCE_USERNAME"))) {
                overrides.put("spring.datasource.username", credentials[0]);
            }
            if (credentials.length == 2 && !StringUtils.hasText(environment.getProperty("SPRING_DATASOURCE_PASSWORD"))) {
                overrides.put("spring.datasource.password", credentials[1]);
            }
        }

        environment.getPropertySources().addFirst(new MapPropertySource("databaseUrlOverrides", overrides));
    }

    @Override
    public int getOrder() {
        return Ordered.HIGHEST_PRECEDENCE;
    }
}
