package com.generatecloud.app.config;

import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;
import static org.assertj.core.api.Assertions.*;

class EksInfrastructureValidationTests {
    @Test void profileCannotBypassAwsGuards() {
        MockEnvironment env = new MockEnvironment(); env.setActiveProfiles("eks");
        assertThatThrownBy(() -> EksInfrastructureValidation.validate(env)).hasMessageContaining("both aws,eks");
    }
    @Test void apiCannotMigrateOrSeedAtRuntime() {
        MockEnvironment env = new MockEnvironment(); env.setActiveProfiles("aws", "eks");
        env.withProperty("spring.flyway.enabled", "true");
        assertThatThrownBy(() -> EksInfrastructureValidation.validate(env)).hasMessageContaining("spring.flyway.enabled=false");
    }
    @Test void enabledSearchCannotRelabelTheFixedModelWeights() {
        MockEnvironment env = new MockEnvironment(); env.setActiveProfiles("aws", "eks");
        env.withProperty("spring.flyway.enabled", "false").withProperty("spring.jpa.hibernate.ddl-auto", "validate")
                .withProperty("server.shutdown", "graceful").withProperty("app.seed.enabled", "false")
                .withProperty("app.pipeline.search-enabled", "true").withProperty("app.pipeline.model-version", "invented-model-v2");
        assertThatThrownBy(() -> EksInfrastructureValidation.validate(env)).hasMessageContaining("clip-vit-b32-openai-v1");
    }
}
