package com.generatecloud.app.config;

import org.springframework.beans.factory.config.BeanFactoryPostProcessor;
import org.springframework.beans.factory.config.ConfigurableListableBeanFactory;
import org.springframework.context.EnvironmentAware;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;
import org.springframework.core.env.Environment;
import org.springframework.core.env.Profiles;

/** EKS is an AWS compute route; it must never bypass the existing AWS guard. */
@Configuration(proxyBeanMethods = false)
@Profile("eks")
public class EksInfrastructureValidation implements BeanFactoryPostProcessor, EnvironmentAware {
    private Environment environment;
    public void setEnvironment(Environment environment) { this.environment = environment; }
    public void postProcessBeanFactory(ConfigurableListableBeanFactory beanFactory) { validate(environment); }
    static void validate(Environment env) {
        if (!env.acceptsProfiles(Profiles.of("aws"))) throw new IllegalStateException("EKS requires both aws,eks profiles");
        require(env, "spring.flyway.enabled", "false");
        require(env, "spring.jpa.hibernate.ddl-auto", "validate");
        require(env, "server.shutdown", "graceful");
        require(env, "app.seed.enabled", "false");
        if (env.getProperty("app.observability.expose-instance-id", Boolean.class, false))
            require(env, "app.deployment.environment", "dev");
        if (env.getProperty("app.pipeline.search-enabled", Boolean.class, false))
            require(env, "app.pipeline.model-version", "clip-vit-b32-openai-v1");
        AwsInfrastructureValidation.validate(env);
    }
    private static void require(Environment env, String property, String expected) {
        if (!expected.equals(env.getProperty(property))) throw new IllegalStateException("EKS requires " + property + "=" + expected);
    }
}
