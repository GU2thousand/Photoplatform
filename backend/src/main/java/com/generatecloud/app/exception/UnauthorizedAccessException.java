package com.generatecloud.app.exception;

public class UnauthorizedAccessException extends ApiException {
    public UnauthorizedAccessException(String message) {
        super(message);
    }
}
