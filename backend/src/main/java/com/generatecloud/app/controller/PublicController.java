package com.generatecloud.app.controller;

import com.generatecloud.app.dto.DashboardStatsResponse;
import com.generatecloud.app.dto.ImagePageResponse;
import com.generatecloud.app.service.AdminService;
import com.generatecloud.app.service.ImageService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/public")
@RequiredArgsConstructor
public class PublicController {

    private final ImageService imageService;
    private final AdminService adminService;

    @GetMapping("/images")
    public ImagePageResponse images(
            @RequestParam(required = false) String query,
            @RequestParam(required = false) String tag,
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "24") int size
    ) {
        return imageService.listPublicImages(query, tag, page, size);
    }

    @GetMapping("/summary")
    public DashboardStatsResponse summary() {
        return adminService.buildStats(false);
    }
}
