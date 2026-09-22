package com.generatecloud.app.config;

import io.micrometer.core.instrument.MeterRegistry;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;
import javax.sql.DataSource;
import net.ttddyy.dsproxy.ExecutionInfo;
import net.ttddyy.dsproxy.QueryInfo;
import net.ttddyy.dsproxy.listener.QueryExecutionListener;
import net.ttddyy.dsproxy.support.ProxyDataSourceBuilder;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.context.ApplicationListener;
import org.springframework.context.event.ContextRefreshedEvent;
import org.springframework.stereotype.Component;

/** Aggregate timings only: SQL text, bindings, credentials, and tenant IDs never become labels. */
@Component
public class DatabaseMetricsConfig implements BeanPostProcessor, ApplicationListener<ContextRefreshedEvent> {
    private final ObjectProvider<MeterRegistry> registryProvider;
    private volatile MeterRegistry registry;

    public DatabaseMetricsConfig(ObjectProvider<MeterRegistry> registryProvider) {
        this.registryProvider = registryProvider;
    }
    public void onApplicationEvent(ContextRefreshedEvent event) {
        registry = registryProvider.getIfAvailable();
    }
    public Object postProcessAfterInitialization(Object bean, String name) {
        return bean instanceof DataSource source ? instrument(source, () -> registry) : bean;
    }

    static DataSource instrument(DataSource source, Supplier<MeterRegistry> registries) {
        return ProxyDataSourceBuilder.create(source).name("photoplatform")
                .listener(new QueryExecutionListener() {
                    public void beforeQuery(ExecutionInfo execution, List<QueryInfo> queries) {}
                    public void afterQuery(ExecutionInfo execution, List<QueryInfo> queries) {
                        MeterRegistry metrics = registries.get();
                        if (metrics == null) return; // Do not force meter bean creation during Flyway startup.
                        metrics.timer("database_query_duration", "outcome", execution.isSuccess() ? "success" : "error")
                                .record(execution.getElapsedTime(), TimeUnit.MILLISECONDS);
                        if (!execution.isSuccess()) metrics.counter("database_query_errors").increment();
                        if (execution.getElapsedTime() >= 1000) metrics.counter("database_slow_queries").increment();
                    }
                }).afterMethod(execution -> {
                    if (execution.getMethod().getName().equals("getConnection") && execution.getThrown() != null) {
                        MeterRegistry metrics = registries.get();
                        if (metrics != null) metrics.counter("database_connection_failures").increment();
                    }
                }).build();
    }
}
