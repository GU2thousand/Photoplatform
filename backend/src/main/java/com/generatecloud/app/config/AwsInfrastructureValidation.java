package com.generatecloud.app.config;

import org.springframework.beans.BeansException;
import org.springframework.beans.factory.config.BeanFactoryPostProcessor;
import org.springframework.beans.factory.config.ConfigurableListableBeanFactory;
import org.springframework.context.EnvironmentAware;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;
import org.springframework.core.env.Environment;

/** Runs before datasource clients are instantiated, preventing unsafe profile fallbacks. */
@Configuration(proxyBeanMethods = false)
@Profile("aws")
public class AwsInfrastructureValidation implements BeanFactoryPostProcessor, EnvironmentAware {
    private Environment environment;
    public void setEnvironment(Environment environment) { this.environment = environment; }
    public void postProcessBeanFactory(ConfigurableListableBeanFactory beanFactory) throws BeansException {
        validate(environment);
    }
    static void validate(Environment environment) {
        require(environment, "app.storage.provider", "aws");
        require(environment, "spring.rabbitmq.ssl.enabled", "true");
        require(environment, "spring.rabbitmq.ssl.validate-server-certificate", "true");
        require(environment, "spring.rabbitmq.ssl.verify-hostname", "true");
        require(environment, "spring.datasource.hikari.data-source-properties.sslmode", "verify-full");
        require(environment, "app.seed.enabled", "false");
        String jwt = environment.getRequiredProperty("app.jwt.secret");
        if (jwt.length() < 32 || jwt.contains("generate-cloud-demo") || jwt.contains("please-change") || jwt.contains("change-me"))
            throw new IllegalStateException("AWS requires a non-demo APP_JWT_SECRET of at least 32 characters");
        String url = environment.getRequiredProperty("spring.datasource.url");
        validateJdbcUrl(url, false);
        String flywayUrl = environment.getProperty("spring.flyway.url");
        if (flywayUrl != null || environment.getProperty("spring.flyway.user") != null
                || environment.getProperty("spring.flyway.password") != null)
            validateJdbcUrl(flywayUrl == null ? url : flywayUrl, true);
    }
    private static void validateJdbcUrl(String url, boolean independentConnection) {
        if (!url.startsWith("jdbc:postgresql://")) throw new IllegalStateException("AWS requires a PostgreSQL JDBC datasource URL");
        // JDBC URL parameters override connection properties; reject a TLS downgrade there too.
        String query = url.contains("?") ? url.substring(url.indexOf('?') + 1) : "";
        boolean verifiesTls = false;
        for (String parameter : query.split("&")) {
            String[] pair = parameter.split("=", 2);
            String key = java.net.URLDecoder.decode(pair[0], java.nio.charset.StandardCharsets.UTF_8).toLowerCase(java.util.Locale.ROOT);
            String value = pair.length == 2 ? java.net.URLDecoder.decode(pair[1], java.nio.charset.StandardCharsets.UTF_8) : "";
            if (key.equals("sslmode") && value.equals("verify-full")) verifiesTls = true;
            if ((key.equals("sslmode") && !value.equals("verify-full")) || key.equals("sslfactory")
                    || key.equals("sslhostnameverifier") || (key.equals("ssl") && !value.equals("true")))
                throw new IllegalStateException("AWS datasource URL must not override TLS certificate/hostname verification");
        }
        if (independentConnection && !verifiesTls)
            throw new IllegalStateException("Dedicated Flyway JDBC URL must include sslmode=verify-full and the RDS sslrootcert path");
    }
    private static void require(Environment environment, String name, String expected) {
        if (!expected.equals(environment.getProperty(name)))
            throw new IllegalStateException("AWS profile requires " + name + "=" + expected);
    }
}
