// Copyright (c) 2026, Quantbit Technolgies Pvt. Ltd. and contributors
// For license information, please see license.txt



frappe.ui.form.on('Biometric Attendance', {
	from_date: function(frm) {
		frm.call({
			method:"check_dates",
			doc:frm.doc
		})
	},

	to_date: function(frm) {
		frm.call({
			method:"check_dates",
			doc:frm.doc
		})
	},

	sync: async function (frm) {
        frappe.msgprint("Syncing Biometric Attendance Data. Please wait...by gayatri");
		if (!frm.doc.from_date) {
			frappe.msgprint(__('Please select From Date'));
			return;
		}
		if (!frm.doc.to_date) {
			frappe.msgprint(__('Please select To Date'));
			return;
		}
		frappe.confirm(
			__('Are you sure you want to proceed?'),
			async () => {
				try {
					const r = await frappe.call({
						method: "face_biometric_integration.face_biometric_integration.face_biometric_integration.doctype.biometric_attendance.biometric_attendance.enqueue_details",
						args: {
							docname: frm.doc.name
						},
						freeze: true,
						freeze_message: __("Biometric Attendance Data Processing..."),
					});

				} catch (err) {
					frappe.msgprint({
						title: __("Error"),
						message: __("Something went wrong while calculation."),
						indicator: "red"
					});
					console.error(err);
				}
			},
			() => {
				console.log('Action canceled');
			}
		);
	},
});