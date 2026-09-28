package com.generatecloud.app.config;

import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.*;
import static org.assertj.core.api.Assertions.*;

class InstanceEvidenceFilterTests {
    @Test void recordsActualPodUidAndSourceRevisionOnDevResponse() throws Exception {
        String uid=UUID.randomUUID().toString(); String sha="a".repeat(40);
        var response=new MockHttpServletResponse();
        new InstanceEvidenceFilter(uid,sha).doFilter(new MockHttpServletRequest(),response,new MockFilterChain());
        assertThat(response.getHeader("X-Photoplatform-Pod-Uid")).isEqualTo(uid);
        assertThat(response.getHeader("X-Photoplatform-Revision")).isEqualTo(sha);
    }
    @Test void labelsCannotMasqueradeAsActualRuntimeIdentity() {
        assertThatThrownBy(()->new InstanceEvidenceFilter("deployment-name","a".repeat(40))).isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(()->new InstanceEvidenceFilter(UUID.randomUUID().toString(),"main")).isInstanceOf(IllegalStateException.class);
    }
}
