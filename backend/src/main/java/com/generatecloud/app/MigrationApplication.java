package com.generatecloud.app;

import java.util.Map;
import org.flywaydb.core.Flyway;

/** Deliberately no Spring context: no HTTP server, seed, scheduler or MQ client. */
public final class MigrationApplication {
    private MigrationApplication() {}

    public static void main(String[] args) {
        migrate(System.getenv()); // Any exception escapes main and produces non-zero exit.
        System.out.println("Migration completed and validated; application services were not started.");
    }

    static void migrate(Map<String, String> env) {
        String url = required(env, "MIGRATOR_DATABASE_URL");
        String user = required(env, "MIGRATOR_DATABASE_USERNAME");
        String password = required(env, "MIGRATOR_DATABASE_PASSWORD");
        validateConfiguration(env, url);
        Flyway flyway = Flyway.configure().dataSource(url, user, password)
                .locations("classpath:db/migration").baselineOnMigrate(true).baselineVersion("0")
                .cleanDisabled(true).load();
        flyway.migrate();
        flyway.validate();
    }

    static void validateConfiguration(Map<String, String> env, String url) {
        if (!url.startsWith("jdbc:postgresql://"))
            throw new IllegalStateException("Migrator requires a PostgreSQL JDBC URL");
        String profiles = env.getOrDefault("SPRING_PROFILES_ACTIVE", "");
        boolean aws = java.util.Arrays.asList(profiles.split(",")).contains("aws");
        boolean eks = java.util.Arrays.asList(profiles.split(",")).contains("eks");
        if (eks && !aws) throw new IllegalStateException("EKS migration requires both aws,eks profiles");
        if (aws) {
            com.generatecloud.app.config.AwsInfrastructureValidation.validateJdbcUrl(url, true);
        }
    }

    private static String required(Map<String, String> env, String name) {
        String value = env.get(name);
        if (value == null || value.isBlank()) throw new IllegalStateException("Missing " + name);
        return value;
    }
}
