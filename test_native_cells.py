"""Native-grid selection and display alignment; only temporary synthetic data."""
from io import BytesIO
import asyncio
import unittest
from types import SimpleNamespace

import numpy as np
from PIL import Image

import crafts_tod_native_web as web
from crafts_tod_mask import MaskDocument, build_mask
import test_native_navigation as navigation


class CellSelectionTests(unittest.TestCase):
    setUp = navigation.NavigationTests.setUp
    aggregate = navigation.NavigationTests.aggregate

    def payload(self, **changes):
        value = dict(mode='box', pol=0, both=False, cells=dict(c0=1,c1=2,r0=1,r1=2),
                     group_key=self.state.group.key, revision=self.state.revision,
                     view_mode='mean', beam='M01')
        value.update(changes)
        return value

    def test_single_cell_including_first_and_last_exports_exactly_one_sample(self):
        for c,r in [(0,0),(1,1),(3,2)]:
            self.state.document=MaskDocument.empty(self.state.group)
            self.state.edit(self.payload(cells=dict(c0=c,c1=c+1,r0=r,r1=r+1)))
            mask=build_mask(self.state.frequency,3,self.state.document,0)
            expected=np.zeros((3,4),bool);expected[r,c]=True
            np.testing.assert_array_equal(mask,expected)
            self.assertFalse(build_mask(self.state.frequency,3,self.state.document,1).any())

    def test_frequency_both_and_single_beam_scope_are_preserved(self):
        self.state.document=MaskDocument.empty(self.state.group)
        self.state.view_mode='single'
        self.state.edit(self.payload(mode='frequency',both=True,view_mode='single'))
        for pol in (0,1):
            self.assertFalse(build_mask(self.state.frequency,3,self.state.document,pol).any())
            mask=build_mask(self.state.frequency,3,self.state.document,pol,'M01')
            self.assertTrue(mask[:,1].all());self.assertEqual(mask.sum(),3)
            self.assertFalse(build_mask(self.state.frequency,3,self.state.document,pol,'M02').any())

    def test_one_channel_round_trips_through_json_and_all_19_npz_products(self):
        self.state.document=MaskDocument.empty(self.state.group)
        self.state.edit(self.payload(mode='frequency',both=True))
        paths=asyncio.run(self.state.save())
        self.assertEqual(len(paths),20)
        for path in paths[1:]:
            with np.load(path,allow_pickle=False) as product:
                expected=np.zeros((3,4,2),dtype=bool);expected[:,1,:]=True
                np.testing.assert_array_equal(product['mask'],expected)
                np.testing.assert_array_equal(product['freq'],self.state.frequency)

    def test_single_short_tail_cannot_be_marked_and_web_save_uses_native_length(self):
        st = self.state
        st.document = MaskDocument.empty(st.group)
        st.aggregate.native_record_counts = {beam: 3 for beam in st.group.beams}
        st.aggregate.native_record_counts['M17'] = 2
        st.view_mode, st.selected_beam = 'single', 'M17'
        def mark(start, stop):
            st.edit(self.payload(view_mode='single', beam='M17', mode='time',
                                 cells=dict(c0=0,c1=4,r0=start,r1=stop)))
        with self.assertRaisesRegex(ValueError, '缺失尾部'):
            mark(2, 3)
        mark(1, 3)
        self.assertEqual(st.document.beam_polarizations['M17'][0].time_ranges_records, [[1, 2]])
        st.view_mode = 'mean'
        st.edit(self.payload(beam='M17', mode='time', pol=1, cells=dict(c0=0,c1=4,r0=2,r1=3)))
        paths = asyncio.run(st.save())
        for path in paths[1:]:
            with np.load(path, allow_pickle=False) as product:
                short = product['beam'].item() == 'M17'
                expected = np.zeros((2 if short else 3, 4, 2), dtype=np.uint8)
                if short:
                    expected[1,:,0] = 1
                else:
                    expected[2,:,1] = 1
                np.testing.assert_array_equal(product['mask'], expected)
                np.testing.assert_array_equal(product['mjd'], st.mjd[:len(expected)])

    def test_full_band_means_and_denominators_follow_scope_and_hold_nan_gaps(self):
        state = self.state
        state.local_medians = [10., 20.]
        values = np.array([[[1, 0], [2, 4], [3, 8], [5, 12]],
                           [[1, 1], [1, 1], [7, np.nan], [9, np.inf]],
                           [[2, 2], [2, 2], [2, 2], [2, 2]]], dtype=np.float32)
        state.aggregate.raw_mean[:] = values
        state.aggregate.auto_masked_mean[:] = values
        state.aggregate.auto_masked_mean[0, 3, 0] = np.nan
        state.document.polarizations[1].time_ranges_records = [[2, 3]]
        payload = dict(group_key=state.group.key, content_revision=0,
                       apply_auto=True, view_mode='mean', view_label='19-beam Mean')

        async def check():
            mean, duplicate = await asyncio.gather(state.band_profile(payload), state.band_profile(payload))
            self.assertIs(mean, duplicate)
            self.assertEqual(mean['valid_channels'], [[1, 2, 2], [4, 2, 0]])
            self.assertEqual(mean['mean_k'], [[13., 18., 12.], [26., 21., None]])
            raw = await state.band_profile(dict(payload, apply_auto=False))
            self.assertEqual((raw['valid_channels'][0][0], raw['mean_k'][0][0]), (2, 14.))
            state.single_medians = [30., 40.]
            state.view_mode = 'single'
            flags = np.zeros(values.shape, dtype=bool)
            flags[0, 2, 0] = True
            state.single = SimpleNamespace(beam='M01', raw=values, union_mask=flags)
            state.document.beam_polarizations['M01'][0].time_ranges_records = [[1, 2]]
            single = await state.band_profile(dict(payload, view_mode='single', view_label='M01'))
            self.assertEqual(single['valid_channels'][0], [1, 0, 2])
            self.assertEqual(single['mean_k'][0], [35., None, 32.])
            self.assertIsNone(await state.band_profile(payload))
            state._spectrum_request_token += 1
            self.assertIsNone(await state.band_profile(dict(payload, view_mode='single', view_label='M01')))

        asyncio.run(check())

    def test_stale_or_invalid_grid_selection_is_rejected_without_mutation(self):
        for change in [dict(revision=-1),dict(group_key='other'),dict(view_mode='single'),
                       dict(cells=dict(c0=-1,c1=2,r0=1,r1=2)),
                       dict(cells=dict(c0=1.5,c1=2,r0=1,r1=2)),dict(pol=3)]:
            before=web._jsonable_document(self.state.document)
            with self.assertRaises(ValueError):self.state.edit(self.payload(**change))
            self.assertEqual(web._jsonable_document(self.state.document),before)

    def test_nonuniform_grid_and_fractional_zoom_color_cells_align(self):
        frequency=np.array([100.,101.,104.])
        edges=web.frequency_cell_edges(frequency)
        np.testing.assert_array_equal(edges,[99.5,100.5,102.5,105.5])
        values=np.tile(np.array([0.,.5,1.]),(2,1))
        payload=web.render_view_image(values,None,y0=0,y1=2,x0=0,x1=3,width=12,height=4,
            contrast=0,vmin=0,vmax=1,cell_view=(edges,100.,103.,.25,1.75))[0]
        image=Image.open(BytesIO(payload));self.assertEqual(image.format,'PNG')
        expected=web.VIRIDIS[np.array([0,0,128,128,128,128,128,128,128,128,255,255])]
        np.testing.assert_array_equal(np.asarray(image)[0],expected)

    def test_native_nan_and_auto_mask_cells_remain_neutral(self):
        f=np.array([100.,101.,102.]);v=np.ones((2,3));mask=np.zeros((2,3),bool);mask[1,1]=True
        payload=web.render_view_image(v,mask,y0=0,y1=2,x0=0,x1=3,width=12,height=8,
            contrast=0,auto_width=0,value_mask=mask,auto_total_beams=1,
            cell_view=(web.frequency_cell_edges(f),99.5,102.5,0,2))[0]
        image=np.asarray(Image.open(BytesIO(payload)))
        np.testing.assert_array_equal(image[1,5],web.INVALID_RGB)
        self.assertFalse(np.array_equal(image[5,5],web.INVALID_RGB))

    def test_frequency_zoom_keeps_auto_overlay_on_native_columns_while_records_are_compressed(self):
        frequency=np.arange(24,dtype=float);edges=web.frequency_cell_edges(frequency)
        # A fractional viewport clips the first/last cells, like a wheel zoom.
        f0,f1=1.27,21.27;width,height=1374,256
        x0=int(np.searchsorted(edges,f0,side='right')-1);x1=int(np.searchsorted(edges,f1,side='left'))
        values=np.zeros((2048,24));auto=np.zeros_like(values,dtype=np.uint8);auto[:,4]=19
        payload=web.render_view_image(values,auto,y0=0,y1=2048,x0=x0,x1=x1,
            width=width,height=height,contrast=0,auto_width=0,vmin=0,vmax=1,
            cell_view=(edges,f0,f1,0,2048))[0]
        image=Image.open(BytesIO(payload))
        pixels=np.asarray(image)[height//2]
        coordinates=f0+(np.arange(width)+.5)*(f1-f0)/width
        expected=(coordinates>=3.5)&(coordinates<4.5)
        np.testing.assert_array_equal(pixels[:,0]>100,expected)
        self.assertEqual(image.format,'PNG','resolve either native axis without lossy color bleeding')

    def test_record_zoom_keeps_auto_overlay_on_native_rows_while_channels_are_compressed(self):
        frequency=np.arange(2048,dtype=float);edges=web.frequency_cell_edges(frequency)
        t0,t1=1.27,21.27;width,height=256,400
        values=np.zeros((24,2048));auto=np.zeros_like(values,dtype=np.uint8);auto[4,:]=19
        payload=web.render_view_image(values,auto,y0=1,y1=22,x0=0,x1=2048,
            width=width,height=height,contrast=0,auto_width=0,vmin=0,vmax=1,
            cell_view=(edges,-.5,2047.5,t0,t1))[0]
        image=Image.open(BytesIO(payload));pixels=np.asarray(image)[:,width//2]
        coordinates=t1-(np.arange(height)+.5)*(t1-t0)/height
        expected=(coordinates>=4)&(coordinates<5)
        np.testing.assert_array_equal(pixels[:,0]>100,expected)

    def test_overview_pooling_still_retains_a_single_flagged_sample(self):
        f=np.arange(128,dtype=float);auto=np.zeros((128,128),dtype=np.uint8);auto[63,63]=19
        payload=web.render_view_image(np.zeros((128,128)),auto,y0=0,y1=128,x0=0,x1=128,
            width=32,height=32,contrast=0,auto_width=0,vmin=0,vmax=1,
            cell_view=(web.frequency_cell_edges(f),-.5,127.5,0,128))[0]
        pixels=np.asarray(Image.open(BytesIO(payload)))
        self.assertTrue(np.any(pixels[:,:,0]>100),'overview must not drop narrow automatic flags')

    def test_resized_preview_dilates_without_modifying_the_automatic_flags(self):
        f=np.arange(31,dtype=float)
        counts=np.zeros((21,31),dtype=np.uint8)
        counts[8,14]=19
        counts.flags.writeable=False
        for size in [(24,17),(60,48)]:
            result=web.render_view_image(np.zeros(counts.shape),counts,y0=0,y1=21,x0=0,x1=31,
                width=size[0],height=size[1],contrast=0,auto_width=1,
                cell_view=(web.frequency_cell_edges(f),-.5,30.5,0,21))
            self.assertEqual(Image.open(BytesIO(result[0])).size,size)
        self.assertEqual(np.count_nonzero(counts),1)

    def probe(self, **changes):
        params=dict(group_key=self.state.group.key,revision=self.state.revision,view_mode=self.state.view_mode,
                    beam=self.state.selected_beam,c=1,r=1,pol=0,apply_auto=1)
        params.update(changes)
        return self.state.cell_payload(params)

    def test_probe_reports_display_and_restored_values_with_auto_and_manual_state(self):
        self.state.local_medians=[20.,21.]
        self.state.aggregate.raw_mean[1,1,0]=5.25
        self.state.aggregate.auto_masked_mean[1,1,0]=1.5
        self.state.aggregate.union_flagged_beam_count[1,1,0]=3
        auto=self.probe();raw=self.probe(apply_auto=0)
        self.assertEqual((auto['delta_k'],auto['temperature_k']),(1.5,21.5))
        self.assertEqual((raw['delta_k'],raw['temperature_k']),(5.25,25.25))
        self.assertTrue(auto['manual_bad']);self.assertEqual(auto['auto_flagged_beams'],3)
        self.assertEqual(auto['total_beams'],19)
        self.assertEqual(self.state.revision,4,'hover must never change annotations')

    def test_single_beam_probe_honours_automatic_mask_and_does_not_use_mean_offset(self):
        self.state.view_mode='single';self.state.local_medians=[99.,99.]
        raw=np.full((3,4,2),6.5);mask=np.zeros_like(raw,dtype=bool);mask[1,1,0]=True
        self.state.single=SimpleNamespace(beam='M01',raw=raw,union_mask=mask)
        self.state.single_medians=[19.,22.]
        self.assertIsNone(self.probe()['delta_k']);self.assertIsNone(self.probe()['temperature_k'])
        result=self.probe(apply_auto=0)
        self.assertEqual(result['temperature_k'],25.5);self.assertEqual(result['total_beams'],1)

    def test_probe_nan_and_stale_context_are_explicit(self):
        self.state.aggregate.auto_masked_mean[1,1,0]=np.nan
        self.assertIsNone(self.probe()['delta_k'])
        for change in [dict(c=4),dict(r=-1),dict(pol=2),dict(revision=-1),dict(group_key='wrong'),dict(beam='M02')]:
            with self.assertRaises(ValueError):self.probe(**change)

    def test_frequency_metadata_is_reused_and_invalidated_on_axis_replacement(self):
        edges=self.state.frequency_metadata()[0]
        self.assertIs(edges,self.state.frequency_metadata()[0])
        self.state.frequency=self.state.frequency+10
        replacement=self.state.frequency_metadata()[0]
        self.assertIsNot(edges,replacement)
        np.testing.assert_array_equal(replacement,edges+10)

    def test_display_scale_uses_unmarked_native_cells_and_retains_the_displayed_pixels(self):
        frequency=np.array([100.,101.,103.,107.])
        values=np.array([[1.,2.,3.,4000.],[4.,5.,8000.,4000.],[90.,90.,90.,4000.]])
        layer=web.PolarizationMask(frequency_ranges_mhz=[[107.,107.]],time_ranges_records=[[2,3]])
        extra=web.PolarizationMask(boxes=[dict(frequency_mhz=[103.,103.], records=[1,2])])
        single_auto=np.zeros(values.shape,bool);single_auto[0,2]=True
        selected=web.display_scale_samples(values,np.array([0,0,1,2,2]),np.array([0,0,1,2,3]),
                                           frequency,[layer,extra],single_auto)
        np.testing.assert_array_equal(selected,[1.,2.,4.,5.])
        kwargs=dict(y0=0,y1=3,x0=0,x1=4,width=4,height=3,contrast=0,vmin=0,vmax=5000,
                    cell_view=(web.frequency_cell_edges(frequency),99.5,109.,0,3),value_mask=single_auto)
        standard=web.render_view_image(values,None,**kwargs)
        clean=web.render_view_image(values,None,scale_selection=(frequency,[layer,extra]),**kwargs)
        self.assertEqual(clean[:3],standard[:3])  # Identical fixed limits and identical encoded pixels.
        self.assertLess(clean[4],10);self.assertGreater(standard[4],1000)
        all_bad=web.PolarizationMask(time_ranges_records=[[0,3]])
        self.assertEqual(web.render_view_image(values,None,scale_selection=(frequency,[all_bad]),
                                              with_scale_count=True,**kwargs)[-1],0)


if __name__=='__main__':unittest.main()
