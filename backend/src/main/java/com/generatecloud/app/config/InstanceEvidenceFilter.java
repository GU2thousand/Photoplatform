package com.generatecloud.app.config;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/** Optional EKS-dev ingress evidence; disabled by default and forbidden in prod. */
@Component
@org.springframework.context.annotation.Profile("eks")
@ConditionalOnProperty(name="app.observability.expose-instance-id",havingValue="true")
public class InstanceEvidenceFilter extends OncePerRequestFilter {
    private final String podUid;
    private final String revision;
    public InstanceEvidenceFilter(@Value("${POD_UID:}") String podUid,
                                  @Value("${APP_RELEASE_SHA:}") String revision) {
        if (!podUid.matches("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
                || !revision.matches("[a-f0-9]{40}"))
            throw new IllegalStateException("Instance evidence requires actual Pod UID and full release SHA");
        this.podUid=podUid; this.revision=revision;
    }
    @Override protected void doFilterInternal(HttpServletRequest request,HttpServletResponse response,FilterChain chain)
            throws ServletException, IOException {
        response.setHeader("X-Photoplatform-Pod-Uid",podUid);
        response.setHeader("X-Photoplatform-Revision",revision);
        chain.doFilter(request,response);
    }
}
