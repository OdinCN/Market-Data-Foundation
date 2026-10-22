//go:build !mdf_i4

package main

// Default builds (including v0.0.87 rollback) never register the I-4 candidate.
func I4Dispatch() bool { return false }
