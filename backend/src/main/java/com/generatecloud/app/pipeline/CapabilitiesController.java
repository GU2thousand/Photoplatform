package com.generatecloud.app.pipeline;

import java.util.Map;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

@RestController
@RequiredArgsConstructor
public class CapabilitiesController {
    private final PipelineProperties properties;
    @GetMapping("/api/public/capabilities")
    public Map<String,Object> capabilities() {
        return Map.of("directUpload",properties.isEnabled(),"semanticSearch",properties.isEnabled() && properties.isSearchEnabled(),
                "maxUploadBytes",properties.getMaxBytes(),"searchLanguage","en");
    }
}
