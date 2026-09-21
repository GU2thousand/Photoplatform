package com.generatecloud.app.pipeline;

import com.generatecloud.app.security.AppUserPrincipal;
import com.generatecloud.app.service.AuthService;
import java.util.*;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.*;

@RestController
@RequiredArgsConstructor
@ConditionalOnProperty(name="app.pipeline.enabled",havingValue="true")
public class SearchController {
    private final SearchService search;
    private final AuthService auth;
    @GetMapping({"/api/search","/api/search/semantic"})
    public SearchService.Result search(@AuthenticationPrincipal AppUserPrincipal principal,
            @RequestParam String q,@RequestParam(defaultValue="semantic") String mode,
            @RequestParam(defaultValue="accessible") String scope,@RequestParam(required=false) Long teamId,
            @RequestParam(defaultValue="24") int limit) {
        return search.search(auth.optionalUser(principal),q,mode,scope,teamId,limit);
    }
    @GetMapping("/api/images/{id}/duplicates")
    public List<Map<String,Object>> duplicates(@AuthenticationPrincipal AppUserPrincipal principal,
            @PathVariable long id,@RequestParam(defaultValue="8") int distance) {
        return search.duplicates(auth.requireUser(principal),id,distance);
    }
}
